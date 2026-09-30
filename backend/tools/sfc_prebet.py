"""胜负彩【下注前校验器】—— 对照 2026-09-29 的结案结论逐项体检.

用法:
    PYTHONPATH=. .venv/bin/python tools/sfc_prebet.py            # 最新一期
    PYTHONPATH=. .venv/bin/python tools/sfc_prebet.py 26134      # 指定期号

设计原则: **把能判的判死, 把判不了的明说判不了。**
这个脚本不预测比赛, 只回答「这期符不符合我们已知的那些硬结论」。

结论出处: 记忆 `football-sfc-crowd-concentration` / `football-sfc-prize-mechanics`
          / `football-time-structure-dead-ends`。
"""
import json
import math
import sys

import numpy as np

sys.path.insert(0, ".")

from app.db.session import SessionLocal
from sqlalchemy import text
from tools.crowd_roi import all_probs, Sa, CAP

IDX = {"3": 0, "1": 1, "0": 2}

# ---- 2026-09-29 结案时定下的参照值 (改这些数前先去看记忆) ----
PRIZE_RETURN = 0.6400        # 返奖率写死 64.00% -> 抽水 36.00%, 20 年不变
VIG = 1 - PRIZE_RETURN
TIER1_SHARE = 0.448          # 一等奖占销量比例
BOLD_P = 0.70                # 「胆」的阈值
BOLD_MEDIAN = 1              # 历史上每期 >=0.70 的场次中位数
BASE_HIT = (7.53, 14)        # 全押市场热门 158 期的平均命中
BASE_ZERO = 13               # 158 期里有 13 期一场没中
A_LO, A_HI = 1.2, 1.5        # a 的区间 (Poisson MLE 1.49 / 直接回归 1.18, 逐年 1.12~1.93)
ROI_LO, ROI_HI = -0.50, -0.31  # 对应 a 区间的最优 ROI
CALIB_K = 3.0                # 🔴 实测修正: P~6e-7 处模型说 2.3 注、实际 6.9 注


def q_prop(o):
    q = [1.0 / x for x in o]
    s = sum(q)
    return [x / s for x in q]


def estimate_T(info, recent):
    """本期总票数 T。单注 2 元 ⇒ T = 销量/2。

    🔴 别在销量上再除 0.192 —— 0.192 是【二等奖占销量的比例】，那个 /0.192/2 只在
       手上拿到的是二等【奖金池】时才成立。sfc_issues.total_sale_amount 存的是上游
       totalSaleAmount，本来就是销量。多除一次会把 T 放大约 5 倍。
       2026-09-30 全量实测 n=1760 期：一等池/(0.448*销量) 中位偏差 0.0007%、
       二等池/(0.192*销量) 中位偏差 0.0044% —— 销量就是销量。
       （/0.192 该用的地方是：手上拿到的是【奖金池】时才 池/0.192 = 销量。）
    与 app/services/sfc_check_service.py:_estimate_T 是同一口径（孪生）。
    """
    own = float(info["total_sale_amount"]) if info and info.get("total_sale_amount") else None
    if own:
        return own / 2.0, "own"
    if not recent:
        return None, None
    return sorted(recent)[len(recent) // 2] / 2.0, "median5"


def load(issue=None):
    db = SessionLocal()
    if issue is None:
        issue = db.execute(text("SELECT MAX(issue) FROM sfc_issue_matches WHERE issue>'26000'")).scalar()
    info = db.execute(text("""SELECT issue, sale_end, total_sale_amount, lottery_prize_json
        FROM sfc_issues WHERE issue=:i"""), {"i": issue}).mappings().first()
    # 🔴 庄家盘那三列用【标量子查询】不用 LEFT JOIN —— LEFT JOIN 一旦 predictions 里
    #    出现重复 fixture_id 就会把行数悄悄翻倍, 而【行数 = sfc 场次数】是后面所有
    #    组合层计算的地基(3^14 枚举 / DP)。标量子查询在结构上不可能改行数。
    #    与 app/services/sfc_check_service.py:_load 是同一口径（孪生）。
    rows = db.execute(text("""SELECT m.no, m.home, m.away, m.match_date, m.kickoff_at,
            m.had_h, m.had_d, m.had_a, m.cz_result, m.fixture_id,
            (SELECT p.odds_home FROM predictions p WHERE p.fixture_id=m.fixture_id) AS odds_home,
            (SELECT p.odds_draw FROM predictions p WHERE p.fixture_id=m.fixture_id) AS odds_draw,
            (SELECT p.odds_away FROM predictions p WHERE p.fixture_id=m.fixture_id) AS odds_away
        FROM sfc_issue_matches m WHERE m.issue=:i ORDER BY m.no"""), {"i": issue}).mappings().all()
    recent = [float(r[0]) for r in db.execute(text("""SELECT total_sale_amount FROM sfc_issues
        WHERE total_sale_amount IS NOT NULL AND issue<:i ORDER BY issue DESC LIMIT 5"""),
        {"i": issue}).fetchall()]
    db.close()
    return issue, info, rows, recent


# ---- [5] 复式单 ---- 🔴 与 app/services/sfc_check_service.py 是同一套算式（孪生）。
# 改了任一份必须同步另一份，并且【两边跑同一期对数字】。这里是 CLI 侧的实现。
SYM = ("3", "1", "0")                        # 场内序号 0/1/2 -> 彩果记法
TICKET_BUDGET_LADDER = (128, 512, 2048)      # 元
TICKET_A_GRID = (A_LO, 1.49, A_HI)
TICKET_NOTES_MAX = 200_000


def _ticket_cands(q):
    order = np.argsort(-np.asarray(q, dtype=float))
    return [(k, sorted(int(x) for x in order[:k]), float(sum(q[j] for j in order[:k])))
            for k in (1, 2, 3)]


def _ticket_sets(ps, have, budget_yuan):
    """注数 = Prod k_i <= budget/2 下最大化期望命中（= 每场覆盖率之和，可分离 ⇒ DP 精确）。"""
    max_notes = int(budget_yuan // 2)
    dp = {1: (0.0, [])}
    for cand in [_ticket_cands(ps[h[0]]) for h in have]:
        nxt = {}
        for notes, (val, path) in dp.items():
            for k, sel, cov in cand:
                n = notes * k
                if n > max_notes:
                    continue
                v = val + cov
                if n not in nxt or v > nxt[n][0]:
                    nxt[n] = (v, path + [(k, sel, cov)])
        dp = nxt
    if not dp:
        return None
    notes, (_, path) = max(dp.items(), key=lambda kv: kv[1][0])
    return path, notes


def _ticket_prob_arr(Q, path):
    """按 itertools.product 的顺序外积（1 号场最显著）。与 all_probs 的排列不同，但同批数。"""
    arr = np.array([Q[-1][j] for j in path[-1][1]], dtype=float)
    for k in range(len(path) - 2, -1, -1):
        arr = np.concatenate([arr * Q[k][j] for j in path[k][1]])
    return arr


def _ticket_ret(arr, Q, T, a, k_calib):
    """分母 m*k_calib + 1（票内各组合互不相同，没有自稀释）。单注 2 元已内建，别再除 2。"""
    S = float(np.prod([sum(x ** a for x in q) for q in Q]))
    m = (arr ** a / S) * T
    return float((arr * np.minimum(TIER1_SHARE * T / (m * k_calib + 1.0), CAP / 2.0)).mean()) - 1.0


def _ticket_one(ps, have, recent, res, budget_yuan, T):
    got = _ticket_sets(ps, have, budget_yuan)
    if not got:
        return None
    path, notes = got
    if notes > TICKET_NOTES_MAX:
        return None
    Q = [ps[h[0]] for h in have]
    arr = _ticket_prob_arr(Q, path)
    covs = [c[2] for c in path]
    model = [_ticket_ret(arr, Q, T, a, 1.0) for a in TICKET_A_GRID]
    calib = [_ticket_ret(arr, Q, T, a, CALIB_K) for a in TICKET_A_GRID]
    return {"budget": int(budget_yuan), "notes": notes, "cost": notes * 2,
            "face": "/".join("".join(SYM[j] for j in sel) for _, sel, _ in path),
            "hits": sum(covs), "p_all": float(np.prod(covs)),
            "model_lo": min(model), "model_hi": max(model),
            "calib_lo": min(calib), "calib_hi": max(calib),
            "actual": (sum(1 for i, c in enumerate(path) if res[i] in c[1])
                       if res else None)}


def main():
    issue = sys.argv[1] if len(sys.argv) > 1 else None
    issue, info, rows, recent = load(issue)
    if not rows:
        print(f"期 {issue} 没有存档行")
        return
    info = info or {}
    print("=" * 68)
    print(f" 胜负彩下注前校验   期号 {issue}   停售 {info.get('sale_end')}")
    print("=" * 68)
    fails, warns = [], []

    # ---------- [0] 硬门槛 ----------
    print(f"\n[0] 硬门槛: 抽水率")
    print(f"    返奖率 {PRIZE_RETURN:.2%} (一等 {TIER1_SHARE:.1%} + 二等 19.2%, 20 年写死)")
    print(f"    抽水 {VIG:.2%} = 竞彩(11.4%)的 {VIG/0.114:.2f} 倍")
    print(f"    历史最优买法(任意策略) ROI: {ROI_LO:.0%} ~ {ROI_HI:.0%}")
    print(f"    -> 若目标是【赚钱】: 否决。这是主承重墙, 与 a 无关。")
    fails.append("抽水 36.00% 写死")

    # H0-3 【绊线】: T 的定义必须是 销量/2。
    # 🔴 右边直接写成 销量/2 的定义式, 【不许从 estimate_T 的返回值反推】—— 那样
    #    成了同义反复, 什么都查不出来。这一条专门抓「谁换个写法把 T 碰坏了」:
    #    老代码 T = 销量/0.192/2 在这里会给出 4.208 (远超 0.02)。
    #    只有已开奖(销量已知)才判得了; 赛前没有可比对的量, 判 SKIP。
    # 孪生 = app/services/sfc_check_service.py 的 t_consist（规则 H0-3）。
    t0, t0_src = estimate_T(info, recent)
    if t0_src == "own":
        sale = float(info["total_sale_amount"])
        cons = abs(t0 * 2.0 / sale - 1.0)
        ok3 = cons <= 0.02
        print(f"    {'PASS' if ok3 else 'FAIL'} H0-3  T 一致性  |T*2/销量-1| = {cons:.6g}"
              f" <= 0.02   (T={t0:,.0f} 注, 销量={sale:,.0f} 元)")
        if not ok3:
            fails.append(f"T 不是 销量/2 (偏差 {cons:.2%}) —— [3]/[5] 全作废, 先修代码")
    else:
        print(f"    SKIP H0-3  T 一致性  赛前销量未出, 没有可比对的量")

    # ---------- [1] 结构体检 ----------
    print(f"\n[1] 结构体检")
    def _pick(cols):
        o = [(r["no"], r["home"], r["away"], [float(r[c] or 0) for c in cols]) for r in rows]
        return [x for x in o if min(x[3]) > 1.0]

    # 🔴 选盘口径: 优先体彩 had_*（胜负彩是体彩的盘, 它自己的价最贴）。体彩只在
    #    在售窗口开赔、停售即撤盘, 于是历史期常年 6/14、永远算不了 -> 加一层
    #    【整体回退】到庄家 Pinnacle(predictions.odds_*)。
    #    🔴 只能整体切不能逐场混（一半体彩一半庄家 = 组合概率不是一个统一口径）。
    #    判据: 另一套【更全】才换, 平手保持体彩。
    #    ⚠️ 回退改变 P 的来源, 两套盘去水概率不等(devig 是「方法,公司」配对问题)
    #       -> src 必须露在报告面上。
    #    与 app/services/sfc_check_service.py:_odds_metrics 是同一口径（孪生）。
    have = _pick(("had_h", "had_d", "had_a"))
    src = "jc"
    if len(have) < len(rows):
        _alt = _pick(("odds_home", "odds_draw", "odds_away"))
        if len(_alt) > len(have):
            have, src = _alt, "pinnacle"
    _src_txt = "体彩竞彩" if src == "jc" else "庄家Pinnacle(体彩不齐,整体回退)"
    print(f"    赔率覆盖          {len(have)}/14  [{_src_txt}]"
          + ("   <-- 不齐, 组合层无法枚举" if len(have) < 14 else ""))
    if info.get("cz_result") or any(r["cz_result"] for r in rows):
        print(f"    ⚠️ 本期已有赛果 -> 这是【复盘】不是下注前校验")
        warns.append("已开奖")

    ps = {o[0]: q_prop(o[3]) for o in have}
    if ps:
        bold = [no for no, p in ps.items() if max(p) >= BOLD_P]
        order = sorted(ps.items(), key=lambda kv: -max(kv[1]))
        nm = {r["no"]: (r["home"], r["away"]) for r in rows}
        print(f"    胆(单场最大去水概率>={BOLD_P})   {len(bold)} 场   历史中位 {BOLD_MEDIAN} 场")
        for no in bold:
            print(f"         {no:>2} {nm[no][0]} vs {nm[no][1]}   {max(ps[no]):.1%}")
        if len(bold) <= 1:
            print(f"        -> 少于 2 个胆 = 历史上典型的『被迫押掷硬币』结构")
            warns.append("无胆/单胆")
        print(f"    最悬的 3 场(最大概率最低):")
        for no, p in order[-3:]:
            print(f"         {no:>2} {nm[no][0]} vs {nm[no][1]}   最高 {max(p):.1%}")

    # ---------- [2] 选号 (a 不参与) ----------
    print(f"\n[2] 选号 —— 这一节 a 完全【不参与】")
    print(f"    去水用【等比】(体彩是定额比例抽水, 6 档水位极差 0.04pp)")
    print(f"    基准线: 全押市场热门, 158 期中 {BASE_HIT[0]}/{BASE_HIT[1]}, 有 {BASE_ZERO} 期 0 中")
    print(f"    -> 任何选号分析必须先打败这条线, 否则不如不分析")
    print(f"    🔴 禁用『上期冷/热推本期』: 期级自相关 r=+0.040, R^2=0.16%")

    # ---------- [3] 冷门组合路径 ----------
    print(f"\n[3] 押冷门组合 —— 理论上唯一的赚钱路径")
    T3, t_src = estimate_T(info, recent)
    if len(have) < 14 or T3 is None:
        print(f"    跳过: 赔率 {len(have)}/14" + ("" if T3 is not None else ", 估不出 T"))
        if len(have) < 14:
            print(f"    🔴 本节要求 14/14 —— 缺一场就没法枚举组合。补救:")
            # 🔴 编号与表里 S1-1 的 requirement 逐条对齐（2026-09-29 漏同步过一次, 2026-09-30 又
            #    加了 ④⑤, 顺手把这里的编号也掰回表里的顺序 —— 两边编号不同会让人以为漏了一条）。
            print(f"       ① 打开胜负彩页点『存档本期』重拉体彩赔率(抓的是当时的实时价)")
            print(f"       ② 停售后重存档【无效】: 体彩停售即撤盘 -> had_* 全 NULL, 不可补")
            print(f"       ③ ⚠️ `infohub_match_id` 分不出「永远没有」和「窗口没到」: 全表 46662 行")
            print(f"          里 =0 出现 0 次(根本没这个状态), 只有 NULL 与 >0; 而且 NULL 不等于")
            print(f"          「体彩没开」—— 26134 缺的 8 场全 NULL, 其中 7 场那天仍在竞彩售卖中。")
            print(f"          要判只能拿队名去 GET /api/jc/matches 对, 对不上的就别等。")
            print(f"       ④ 体彩不齐时已【整体回退】庄家 Pinnacle(`predictions.odds_*`) —— 见上面 [1]")
            print(f"          那行印的盘口。只有另一套覆盖得更全才切, 平手保持体彩, 绝不逐场混。")
            print(f"       ⑤ 所以「赔率覆盖」这个数 = 【选用那套盘】的覆盖数, 不再恒等于体彩覆盖数;")
            print(f"          用的是庄家盘时, 本期不可与体彩口径的期次直接比数")
            print(f"          (去水水位 Pinnacle 3.7% vs 体彩 13.0%)。")
    else:
        T = T3
        Q = [ps[o[0]] for o in have]
        P = all_probs(Q)
        P = P[P > 0]
        cnt, edges = np.histogram(np.log(P), bins=50000)
        ok = cnt > 0
        grid = np.exp((edges[:-1][ok] + edges[1:][ok]) / 2.0)
        if t_src == "own":
            print(f"    T = 本期销量 {float(info['total_sale_amount']):,.0f} 元 / 2"
                  f" = {T:,.0f} 注（已开奖，真值）")
        else:
            print(f"    T 估计 = 近 5 期中位销量 {sorted(recent)[len(recent)//2]:,.0f} 元 / 2"
                  f" = {T:,.0f} 注")
        print(f"    判据: 彩民在该组合上的票数 m < {TIER1_SHARE} * P * T  (n=1 时)")
        print(f"    {'a':>5}{'最优P':>11}{'模型m(注)':>11}{'模型ROI':>9}{'修正x3后m':>11}{'修正ROI':>9}")
        best_corr = -9
        for a in (A_LO, 1.49, A_HI):
            S = Sa(Q, a)
            rowout = []
            for n in (1, 10, 100):
                mT = (grid ** a / S) * T
                roi_mod = grid * np.minimum(TIER1_SHARE * T / (mT + n), CAP / 2.0) - 1.0
                roi_cal = grid * np.minimum(TIER1_SHARE * T / (mT * CALIB_K + n), CAP / 2.0) - 1.0
                k = int(np.argmax(roi_mod))
                rowout.append((roi_mod[k], roi_cal[k], grid[k], mT[k]))
            b = max(rowout, key=lambda x: x[0])
            best_corr = max(best_corr, max(x[1] for x in rowout))
            print(f"    {a:>5.2f}{b[2]:>11.2e}{b[3]:>11.1f}{b[0]*100:>+8.1f}%"
                  f"{b[3]*CALIB_K:>11.1f}{b[1]*100:>+8.1f}%")
        print(f"    🔴 「修正 x3」的来由: 同一批数据实测, 模型在 P~6e-7 处说彩民押 2.3 注,")
        print(f"       实际 6.9 注(比值 1.49~2.08, 且全表不是平的) -> 模型在最优点上【低估彩民】。")
        if best_corr > 0:
            warns.append("修正后仍为正, 需人工复核")
        else:
            fails.append("冷门路径: 修正后 ROI 仍为负")

    # ---------- [4] 反伪影 ----------
    print(f"\n[4] 若任何一处算出『能赚』, 先过这四问")
    for i, q in enumerate([
        "独中时 payout 被什么封顶? (奖池总量 / 500万单注 / 整数票) —— 三个都会剪",
        "公式里的 m 是不是当成了连续量? 分数票会让 payout/n 在 n->0 时发散",
        "自己的票有没有算进分母? (自稀释)",
        "🔴 这个外推有【直接观测】支持吗? 模型在这段区间做过校准检验吗?",
    ], 1):
        print(f"    {i}. {q}")
    print(f"    (前三条来自 2026-09-29 我连栽三次的 +49.9%/+98098%/+140%; 第 4 条是同日的 +56%)")

    # ---------- [5] 复式单（H0-2 的现场实例化；只算钱，不出选号建议） ----------
    print(f"\n[5] 复式单测算 —— H0-2 的现场实例化")
    if len(have) < 14 or T3 is None:
        print(f"    跳过: 赔率 {len(have)}/14"
              + ("" if T3 is not None else ", 估不出 T"))
        print(f"    🔴 缺一场就分不出组合（同 [3] 节门槛）")
    else:
        T5, _src5 = T3, t_src
        res5 = [IDX.get((r["cz_result"] or "").strip()) for r in rows]
        res5 = res5 if all(x is not None for x in res5) else None
        src5 = "本期销量" if _src5 == "own" else "近 5 期中位销量推算"
        print(f"    T = {T5:,.0f} 注（单注 2 元，{src5}）   "
              f"ROI 仅一等奖(0.448*T)，区间取 a={A_LO}~{A_HI}")
        print(f"    {'预算(元)':>9}{'注数':>7}{'成本(元)':>9}{'期望命中':>9}{'P全中':>10}"
              f"{'实际':>7}{'模型ROI':>16}{'修正x3':>16}")
        for b in TICKET_BUDGET_LADDER:
            t = _ticket_one(ps, have, recent, res5, b, T5)
            if not t:
                print(f"    {b:>9,}   注数超上限或算不出")
                continue
            act = f"{t['actual']}/14" if t["actual"] is not None else "-"
            print(f"    {t['budget']:>9,}{t['notes']:>7,}{t['cost']:>9,}{t['hits']:>9.2f}"
                  f"{t['p_all']:>10.2e}{act:>7}"
                  f"{t['model_lo'] * 100:>9.1f}~{t['model_hi'] * 100:<6.1f}"
                  f"{t['calib_lo'] * 100:>9.1f}~{t['calib_hi'] * 100:<6.1f}")
        print(f"    票面（1..14 场；1 个号=单挑，2 个号=双选，3 个号=全包）：")
        for b in TICKET_BUDGET_LADDER:
            t = _ticket_one(ps, have, recent, res5, b, T5)
            if t:
                print(f"      {t['budget']:>6,} 元 = {t['notes']:>6,} 注   {t['face']}")
        print(f"    🔴 加预算能买到更高期望命中、ROI 也会略向 0 靠，但每一档都远在 0 以下")
        print(f"       —— 指望靠加钱翻正是没有出路的（H0-2）。")
        print(f"       （别和 F4-3 的「买更多只会更差」混起来：那条说的是买一堆冷门组合，")
        print(f"        买越多选到的越差；这里买的是覆盖面，两回事。）")

    # ---------- 结论 ----------
    print(f"\n{'=' * 68}")
    print(f" 校验结论")
    print(f"{'=' * 68}")
    for f in fails:
        print(f"  FAIL  {f}")
    for w in warns:
        print(f"  WARN  {w}")
    print(f"\n  判据: 本脚本只做【体检】不做【预测】。它判不了『这期冷不冷』——")
    print(f"        期级自相关 R^2=0.16%, 这个问题在数据上不存在答案。")
    print(f"  下注前: 结论记进 tools/bet_journal.py; 主推唯一, 赛前不中途改标。")


if __name__ == "__main__":
    main()
