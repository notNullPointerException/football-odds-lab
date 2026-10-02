# -*- coding: utf-8 -*-
"""标准线 —— 用库里的 10 万场赛果标定一条参考线，并做样本外校准检验。

【为什么要有这个】
拿 Pinnacle 当基准算 EV 那条路走不通：查穿三个库确认**没有历史价格**
（odds_snapshots 36 行全是同一天手测残渣、fixture_id 全 NULL；引擎库
bet_journal 只有 8 天且让球腿的 fair_p 一条都没有）。价格过期不补。

但赛果早就在库里：`fixtures` 10 万场已完赛，横跨 2022-01 → 今天。
所以先用赛果标定一条参考线 —— 给定任意盘口线，直接读出真实三路概率。
之后竞彩赔率一到手就能算 EV，**不再依赖任何博彩公司**。

【判据 —— 这个工具存在的意义就是这一条】
经验表必须**跑赢泊松表**。跑不赢 = 标准线没价值，直说，别粉饰。
泊松表就是现在全项目在用的那套（predict.py / jc_combos.lam_hcap /
backtest_heads.margin_dist），在同一批样本上做对照。

三个基准：
    经验表   本工具建的        P(净胜球=k | μ 在桶内)
    泊松表   margin_dist(λh, λa)   ← 现有全套模型的基础
    频率基准 无脑押最常出现那一类

【绝不用 in-sample 自证】
--check 默认走**月度前推**：用 N 月之前的全部赛果建表，只测 N 月。
第 1 个月（前缀不足 min_prefix 场）不计入。

【--leak 是无泄漏自证开关】
leak=True 会把「当前这场自己的比分」先塞进滚动窗口再算 μ。
这是真漏气。跑 `--check` 和 `--check --leak` 对比：
    漏气版 Brier 必须【更低】（偷看了答案，当然更准）。
    差 ~0 = 开关没生效，或有别的隐蔽泄漏路径 —— 先查清楚再信任何数字。

用法:
    .venv/bin/python tools/standard_line.py --build        # 重建表(写库)
    .venv/bin/python tools/standard_line.py                # 打印标准线表(默认)
    .venv/bin/python tools/standard_line.py --show         # 同上，显式写出来
    .venv/bin/python tools/standard_line.py --check        # 样本外校准(只读)
    .venv/bin/python tools/standard_line.py --check --leak # 无泄漏自证
    .venv/bin/python tools/standard_line.py --vs-odds      # 接已积累的竞彩价

🔴 本工具只做参考与展示。闸门①-⑤、三个模型头、任何阈值一行不动。
"""
import argparse
import sys
import unicodedata
from collections import defaultdict

sys.path.insert(0, ".")

from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.services.standard_line_service import (  # noqa: E402
    KM_HI, KM_LO, DEF_BW, DEF_WINDOW, DEF_MIN_PRIOR, MARKET_1X2, MARKET_HHAD,
    ensure_tables, build as sl_build, latest_build, compute_series, bucket_ix,
    bucket_bounds, _load_fixtures, expected_margin, fair,
)
from tools.backtest_heads import margin_dist, brier, norm_cdf  # noqa: E402

LINE = "-" * 100


def w(s):
    """东亚全角字符算 2 宽，用于对齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n, right=False):
    s = str(s)
    fill = " " * max(0, n - w(s))
    return (fill + s) if right else (s + fill)


def pct(v, n=8):
    return pad(f"{v:.1%}" if v is not None else "-", n, right=True)


def norm_cell(dist):
    """桶内计数 -> 归一化概率。"""
    n = sum(dist.values())
    return {k: v / n for k, v in dist.items()}


# ──── 前推：只用 N 月之前的赛果建表，只测 N 月 ────

def walk_forward(ser, min_prefix=5000):
    """月度前推。返回 [(dist, margin, lh, la)]，dist 是【该场之前】的经验分布。"""
    cells = defaultdict(lambda: defaultdict(int))
    frozen, cur, out = None, None, []
    for dt, lh, la, m in ser:
        mo = (dt.year, dt.month)
        if mo != cur:
            cur = mo
            total = sum(sum(d.values()) for d in cells.values())
            frozen = ({ix: norm_cell(d) for ix, d in cells.items()}
                      if total >= min_prefix else None)
        if frozen is not None:
            d = frozen.get(bucket_ix(lh - la))
            if d:
                out.append((d, m, lh, la))
        cells[bucket_ix(lh - la)][min(KM_HI, max(KM_LO, m))] += 1
    return out


def vote(dist, market, line=None):
    """从 {k: p} 读三路。与 standard_line_service.fair 同一套严格不等号。"""
    if market == MARKET_1X2:
        return [sum(p for k, p in dist.items() if k < 0),      # [0] 客胜
                dist.get(0, 0.0),                               # [1] 平
                sum(p for k, p in dist.items() if k > 0)]       # [2] 主胜
    g = float(line)
    return [sum(p for k, p in dist.items() if k + g < 0),
            sum(p for k, p in dist.items() if k + g == 0),
            sum(p for k, p in dist.items() if k + g > 0)]


def idx_1x2(m):
    return 0 if m < 0 else (1 if m == 0 else 2)


def cmd_check(args):
    db = SessionLocal()
    try:
        rows = _load_fixtures(db)
    finally:
        db.close()
    ser = compute_series(rows, leak=args.leak)
    print(f"样本 {len(ser)} 场  {ser[0][0]:%Y-%m-%d} ~ {ser[-1][0]:%Y-%m-%d}")
    print(f"模式: {'LEAK(偷看当前场比分)' if args.leak else 'causal(只用赛前赛果)'}"
          f"   min_prefix={args.min_prefix}")
    wf = walk_forward(ser, args.min_prefix)
    if not wf:
        print("\n前推样本为空 —— min_prefix 太大或数据跨度太短。")
        return
    print(f"前推检验 {len(wf)} 场  (月度前推: 用上月为止的全部赛果建表)")

    # ── [1] 逐格 z 检验：净胜球 = 恰好 k ──
    exp, var, act = defaultdict(float), defaultdict(float), defaultdict(int)
    for dist, m, _lh, _la in wf:
        act[m] += 1
        for k, p in dist.items():
            if abs(k) <= 6:
                exp[k] += p
                var[k] += p * (1 - p)
    print()
    print("[1] 「净胜球 = 恰好 k」逐格检验（经验表预测 vs 实际）")
    print(f"    {pad('k', 5)}{pad('实际', 9)}{pad('经验期望', 11)}{pad('差', 10)}"
          f"{pad('倍数', 8)}{pad('z', 8)}{pad('单尾P', 8)}")
    print("    " + "-" * 62)
    for k in range(6, -7, -1):
        a, e = act.get(k, 0), exp.get(k, 0.0)
        sd = var.get(k, 0.0) ** 0.5
        z = (a - e) / sd if sd > 0 else 0.0
        p1 = (1 - norm_cdf(z)) if z > 0 else norm_cdf(z)
        flag = "  <<< 让平所在格" if abs(k) == 1 else ""
        print(f"    {k:<5}{a:<9}{e:<11.1f}{a - e:<+10.1f}"
              f"{(a / e if e else 0):<8.2f}{z:<+8.2f}{p1:<.4f}{flag}")

    # ── [2] 三表对照（胜平负 Brier）──
    tot_std = tot_poi = tot_fre = 0.0
    freq = defaultdict(int)
    for _dist, m, _lh, _la in wf:
        freq[idx_1x2(m)] += 1
    nf = sum(freq.values())
    fre_vec = [freq[i] / nf for i in range(3)]

    print()
    print("[2] 胜平负 Brier 对照（越低越好）  同一批样本、同一个实际结果")
    for dist, m, lh, la in wf:
        i = idx_1x2(m)
        tot_std += brier([vote(dist, MARKET_1X2)], i)
        tot_poi += brier([vote(margin_dist(lh, la), MARKET_1X2)], i)
        tot_fre += brier([fre_vec], i)
    n = len(wf)
    print(f"    {pad('经验表(标准线)', 20)}{tot_std / n:<12.4f}")
    print(f"    {pad('泊松表(现有全套)', 20)}{tot_poi / n:<12.4f}")
    print(f"    {pad('频率基准(全押最多)', 20)}{tot_fre / n:<12.4f}")
    d = (tot_poi - tot_std) / n
    print(f"    {pad('经验 - 泊松', 20)}{-d:<+12.4f}"
          f"   {'✅ 经验表更好' if d > 0 else '❌ 经验表没跑赢泊松'}")

    # ── [3] 让球三路（竞彩整数线）──
    print()
    print("[3] 让球三路 · 经验表 vs 泊松表（实际命中率做裁判）")
    print(f"    {pad('线', 7)}{pad('选项', 7)}{pad('n', 8)}{pad('实际', 9)}"
          f"{pad('经验表', 10)}{pad('泊松表', 10)}{pad('经验/实际', 11)}{pad('泊松/实际', 11)}")
    print("    " + "-" * 72)
    for line in (0, -1, 1, -2, 2):
        for si, sel in enumerate(("让负", "让平", "让胜")):
            ns = hit = 0
            accs = accp = 0.0
            for dist, m, lh, la in wf:
                # 每条腿在【每一场】都有输赢（不是只在走盘的那些场）。
                # 让平 = 恰好走盘那一档；分母仍是全部场次，否则分母只剩走盘场，恒 100%。
                a = m + line
                win = (a < 0) if si == 0 else ((a == 0) if si == 1 else (a > 0))
                ns += 1
                hit += 1 if win else 0
                accs += vote(dist, MARKET_HHAD, line)[si]
                accp += vote(margin_dist(lh, la), MARKET_HHAD, line)[si]
            a_, s_, p_ = hit / ns, accs / ns, accp / ns
            print(f"    {line:<+7}{sel:<7}{ns:<8}{a_:<9.1%}{s_:<10.1%}{p_:<10.1%}"
                  f"{(a_ / s_ if s_ else 0):<11.2f}{(a_ / p_ if p_ else 0):.2f}")
    print(f"    (实际/预测 = 1.00 为完美校准；让平那几行是本项目最关心的一档)")


def cmd_build(args):
    ensure_tables()
    st = sl_build(window=args.window, min_prior=args.min_prior,
                  build_id=args.build_id, leak=args.leak)
    print("标准线已重建")
    for k, v in st.items():
        print(f"  {pad(k, 12)} {v}")
    print(f"\n  源数据 {st['n_total']} 场，进表 {st['n_used']} 场，{st['buckets']} 个桶")
    print("  ⚠️ 旧批次没删 —— 对比多批的 build_id 就能看出标准线随数据积累怎么动")


def cmd_show(args):
    db = SessionLocal()
    try:
        meta = latest_build(db)
        if not meta:
            print("还没有标准线，先跑 --build")
            return
        bid = meta["build_id"]
        print("=" * 100)
        print(f"标准线  build_id={bid}  建于 {meta['built_at']:%Y-%m-%d %H:%M}")
        print(f"  源数据 {meta['src_from']} ~ {meta['src_to']}  "
              f"{meta['n_total']} 场（进表 {meta['n_used']}）  窗口 {meta['window_matches']} 场")
        print("=" * 100)

        rs = db.execute(text("""
            SELECT bucket_ix ix, bucket_lo lo, bucket_hi hi, margin_k k, p, n
            FROM std_line_margin WHERE build_id=:b ORDER BY bucket_ix, margin_k
        """), {"b": bid}).mappings().all()
        cells = defaultdict(dict)
        ns = {}
        for r in rs:
            cells[r["ix"]][r["k"]] = float(r["p"])
            ns[r["ix"]] = r["n"]

        print()
        # 🔴 表头一律走 pad()，禁止用 ':<18' —— Python 的 <N 按【字符数】补齐，
        # 数据行按【显示宽度】补齐，混用会让 CJK 表头整体错位（实测 93 vs 81）。
        # 同理 'μ' 这类 East_Asian_Width=A 的模糊宽度字符不进表格（各终端渲染不一）。
        # 数值列右对齐(pct 用 right=True)，表头也跟着右对齐，否则表头贴在数字列最左边。
        print(f"  {pad('mu 桶', 18)}{pad('n', 8)}{pad('主胜', 8, True)}"
              f"{pad('平', 8, True)}{pad('客胜', 8, True)}| "
              f"{pad('让-1胜', 9, True)}{pad('让-1平', 9, True)}{pad('让-1负', 9, True)}")
        print("  " + "-" * 90)
        for ix in sorted(cells):
            d = cells[ix]
            lo, hi = bucket_bounds(ix)
            lab = (f"[{lo:+.2f},{hi:+.2f})" if lo is not None and hi is not None
                   else (f"(<{hi:+.2f})" if lo is None else f"(>={lo:+.2f})"))
            v1 = vote(d, MARKET_1X2)
            vh = vote(d, MARKET_HHAD, -1)
            print(f"  {pad(lab, 18)}{ns[ix]:<8}{pct(v1[2], 8)}{pct(v1[1], 8)}{pct(v1[0], 8)}| "
                  f"{pct(vh[2], 9)}{pct(vh[1], 9)}{pct(vh[0], 9)}")
        print()
        print("  让-1 三路是【整数线】，让平不为 0；半球线的让平恒为 0（构造保证）")
    finally:
        db.close()


def cmd_vs_odds(args):
    """把已积累的竞彩价接上标准线。这是「攒够了没」的计量器。"""
    db = SessionLocal()
    try:
        meta = latest_build(db)
        if not meta:
            print("还没有标准线，先跑 --build")
            return
        bid = meta["build_id"]
        total = db.execute(text("SELECT COUNT(*) FROM odds_snapshots")).scalar() or 0
        print("=" * 100)
        print(f"接已积累的竞彩价   标准线 build_id={bid}")
        print(f"  odds_snapshots 总行数 {total}")
        if not total:
            print("\n  一行都没有 —— 手动跑一轮：POST /api/scheduler/odds_watch/trigger")
            print("  （或 .venv/bin/python tools/odds_watch.py）")
            return

        rows = db.execute(text("""
            SELECT s.fixture_id fid, s.market, s.selection, s.line_val, s.odds,
                   s.jc_prob, s.kickoff_at, s.match_label, s.jc_no
            FROM odds_snapshots s
        """)).mappings().all()
        mapped = [r for r in rows if r["fid"]]
        print(f"  带 fixture_id 的 {len(mapped)} / {total}"
              f"   ← 只有 map_date==businessDate 那天才有值(app/api/jc.py:137)")
        if not mapped:
            print("\n  ❌ 全部配不上 —— 一条都算不了。")
            print("     这是已知限制，不是 bug：jc_map 只存一个日期，其余销售日的"
                  "fixture_id 必然是 NULL。")
            print("     要攒出可比样本，得让抓取那天正好是 jc_map 的日期。")
            return

        cached, out = {}, []
        for r in mapped:
            fid = r["fid"]
            if fid not in cached:
                f = db.execute(text("""
                    SELECT home_id, away_id, date FROM fixtures WHERE id=:i
                """), {"i": fid}).mappings().first()
                kk = r["kickoff_at"] or (f["date"] if f else None)
                cached[fid] = (expected_margin(db, f["home_id"], f["away_id"], kk)
                               if f and kk else None)
            mu = cached[fid]
            if mu is None or r["odds"] is None:
                continue
            mk = MARKET_1X2 if r["market"] == "1x2" else MARKET_HHAD
            line = int(float(r["line_val"])) if r["line_val"] is not None else None
            if mk == MARKET_HHAD and line is None:
                continue
            fp = fair(bid, mu, mk, line=line, db=db)[r["selection"]]
            out.append({**dict(r), "mu": mu, "fair": fp, "ev": float(r["odds"]) * fp - 1})

        if not out:
            print(f"\n  ❌ {len(mapped)} 行有 fixture_id，但一条都算不出 μ"
                  f"（球队历史不足 {DEF_MIN_PRIOR} 场，或该玩法缺线）")
            return

        out.sort(key=lambda r: -r["ev"])
        print(f"\n  可比腿 {len(out)} 条 （参考 EV = 竞彩赔率 x 标准线公平价 - 1）")
        print(f"  {pad('号', 9)}{pad('比赛', 26)}{pad('玩法', 7)}{pad('选择', 7)}"
              f"{pad('线', 6)}{pad('赔率', 8)}{pad('竞彩%', 9)}{pad('标准线%', 10)}EV")
        print("  " + "-" * 92)
        for r in out:
            lv = f"{r['line_val']:+.0f}" if r["line_val"] is not None else "-"
            od = f"{float(r['odds']):.2f}"
            jcp = float(r["jc_prob"]) if r["jc_prob"] is not None else None
            mk = "胜平负" if r["market"] == "1x2" else "让球"
            print(f"  {pad(r['jc_no'] or '-', 9)}{pad((r['match_label'] or '-')[:24], 26)}"
                  f"{pad(mk, 7)}{pad(r['selection'], 7)}{pad(lv, 6)}"
                  f"{pad(od, 8, True)}{pct(jcp, 9)}{pct(r['fair'], 10)}"
                  f"{r['ev'] * 100:+8.1f}%")
        print()
        print(f"  ⚠️ 样本 {len(out)} 条 —— 分辨 10pp 的 ROI 差需要 n≈200-300 腿。"
              f"现在这个数什么都证明不了，先攒。")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="重建标准线表(写库)")
    ap.add_argument("--check", action="store_true", help="样本外校准检验(只读)")
    ap.add_argument("--show", action="store_true", help="打印标准线表(不打任何开关时的默认动作)")
    ap.add_argument("--vs-odds", action="store_true", help="接已积累的竞彩价")
    ap.add_argument("--leak", action="store_true",
                    help="🔴 故意漏气(把当前场比分先塞进窗口)，只用于无泄漏自证")
    ap.add_argument("--build-id", default=None, help="指定 build_id（默认按时间戳）")
    ap.add_argument("--window", type=int, default=DEF_WINDOW)
    ap.add_argument("--min-prior", type=int, default=DEF_MIN_PRIOR)
    ap.add_argument("--min-prefix", type=int, default=5000, help="前推首月至少要有多少场才开测")
    args = ap.parse_args()

    if args.build:
        cmd_build(args)
    elif args.check:
        cmd_check(args)
    elif args.vs_odds:
        cmd_vs_odds(args)
    else:
        cmd_show(args)


if __name__ == "__main__":
    main()
