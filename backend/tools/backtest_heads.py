# -*- coding: utf-8 -*-
"""全历史回测：三个头的标定 + 「恰好赢 N 球」格子检验。

【为什么有这个脚本】
让平的整个论点 = 「整数线上『恰好赢 N 球』这个格子被系统性低估」。
之前的证据只有 12/32 这个 32 条的样本（P=0.055，卡在线上）。
predictions 表没存竞彩让球线，扩不了那个样本 —— 但 λ 头就是泊松网格，
633 场全有，可以直接检验「格子系统性低估」这条结构断言。

【关键测法】
λ 头对每场给出 P(净胜球 = k) 的完整分布。把 633 场加总：
    期望场次 = Σ_i P_λi(margin = k)
    实际场次 = Σ_i 1[margin_i = k]
二者做 Poisson-binomial 检验（各场独立 Bernoulli，方差 = Σ p(1-p)）。
- 若「恰好 ±1」实测 >> 期望  -> 泊松网格确实低估该格 -> 让平论点有结构支撑
- 若标定良好                 -> 12/32 大概率是噪声

用法:
    .venv/bin/python tools/backtest_heads.py
    .venv/bin/python tools/backtest_heads.py --since=2026-09-01
"""
import argparse
import sys
import math

sys.path.insert(0, ".")

from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402

MAXG = 11          # 泊松网格截断，与 jc_combos.py 一致


def pois(k, l):
    return math.exp(-l) * l ** k / math.factorial(k)


def margin_dist(lh, la):
    """返回 dict: 净胜球 k -> P(margin = k)，泊松网格。"""
    d = {}
    for i in range(MAXG):
        for j in range(MAXG):
            p = pois(i, lh) * pois(j, la)
            d[i - j] = d.get(i - j, 0.0) + p
    s = sum(d.values())
    return {k: v / s for k, v in d.items()}


def brier(vecs, actual_idx):
    """vecs = 若干个 3 元概率向量；返回平均 Brier。"""
    s = 0.0
    for pv in vecs:
        s += sum((pv[i] - (1 if i == actual_idx else 0)) ** 2 for i in range(3))
    return s / len(vecs)


def norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def load(since=None):
    db = SessionLocal()
    try:
        sql = """SELECT fixture_id, match_date, home_name, away_name, league_name,
                        win_home, win_draw, win_away, lambda_home, lambda_away,
                        llm_win, actual_home_goals hg, actual_away_goals ag
                 FROM predictions
                 WHERE actual_home_goals IS NOT NULL AND actual_away_goals IS NOT NULL
                   AND lambda_home IS NOT NULL AND lambda_away IS NOT NULL"""
        args = {}
        if since:
            sql += " AND match_date >= :since"
            args["since"] = since
        sql += " ORDER BY match_date"
        return db.execute(text(sql), args).mappings().all()
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    args = ap.parse_args()

    rows = load(args.since)
    n = len(rows)
    print("=" * 100)
    print(f"== 全历史回测   {n} 场有比分+λ 的预测"
          f"   {str(rows[0]['match_date'])[:10]} ~ {str(rows[-1]['match_date'])[:10]}")
    print("=" * 100)

    # ─────────── [1] 三个头的标定 ───────────
    cnt = [0, 0, 0]              # 实际 主/平/客
    mis_p = mis_l = 0
    hit_p = hit_l = hit_g = 0
    n_g = 0
    br_p = br_l = 0.0
    llm_conf = {}                # 置信度分桶 -> [命中, 总数]

    for r in rows:
        hg, ag = r["hg"], r["ag"]
        act = 0 if hg > ag else (1 if hg == ag else 2)
        cnt[act] += 1

        pp = [r["win_home"], r["win_draw"], r["win_away"]]
        if all(v is not None for v in pp):
            s = sum(pp)
            pp = [v / s for v in pp]
            br_p += brier([pp], act)
            hit_p += (pp.index(max(pp)) == act)
            mis_p += 1

        lp = None
        lh, la = r["lambda_home"], r["lambda_away"]
        if lh and la and lh > 0 and la > 0:
            md = margin_dist(lh, la)
            lp = [sum(p for k, p in md.items() if k > 0),
                  md.get(0, 0.0),
                  sum(p for k, p in md.items() if k < 0)]
            br_l += brier([lp], act)
            hit_l += (lp.index(max(lp)) == act)
            mis_l += 1

        if r["llm_win"]:
            n_g += 1
            hit_g += (r["llm_win"] == ["主胜", "平", "客胜"][act])

    freq = [c / n for c in cnt]
    print(f"\n[1] 三个头 vs 频率基准   （实际分布 主{freq[0]:.1%} 平{freq[1]:.1%} 客{freq[2]:.1%}）")
    print(f"  {'头':<8}{'首选命中':<12}{'Brier':<10}{'vs 均匀0.6667':<16}{'vs 频率基准':<14}")
    print("  " + "-" * 70)
    # 频率基准 Brier = Σ 频率_i (1-频率_i)，均匀基准 = 2/3
    b_freq = 1 - sum(f * f for f in freq)
    for name, k, m, b in [("prob", hit_p, mis_p, br_p), ("lam", hit_l, mis_l, br_l)]:
        bb = b / m
        print(f"  {name:<8}{k/m:<12.1%}{bb:<10.4f}"
              f"{('优 ✓' if bb < 0.6667 else '劣 ✗'):<16}"
              f"{('优 ✓' if bb < b_freq else '劣 ✗'):<14}")
    if n_g:
        print(f"  {'LLM':<8}{hit_g/n_g:<12.1%}")
    print(f"  {'频率基准':<8}{max(freq):<12.1%}{b_freq:<10.4f}")
    print(f"  {'均匀基准':<8}{'—':<12}{0.6667:<10.4f}")

    # ─────────── [2] 「恰好赢 N 球」格子检验 ───────────
    print(f"\n[2] 🔴 「净胜球 = 恰好 k」格子检验 —— 让平论点的直接检验")
    print(f"    让平在 ±1 格。若实测 >> 期望，泊松网格确实低估该格。")
    exp = {}
    act_cnt = {}
    var = {}
    for r in rows:
        hg, ag = r["hg"], r["ag"]
        m = hg - ag
        act_cnt[m] = act_cnt.get(m, 0) + 1
        lh, la = r["lambda_home"], r["lambda_away"]
        if not (lh and la and lh > 0 and la > 0):
            continue
        md = margin_dist(lh, la)
        for k, p in md.items():
            if abs(k) <= 6:
                exp[k] = exp.get(k, 0.0) + p
                var[k] = var.get(k, 0.0) + p * (1 - p)

    print(f"\n  {'净胜球':<8}{'实际场次':<10}{'λ期望场次':<12}{'实际-期望':<12}{'倍数':<9}{'z':<8}{'单尾P':<9}")
    print("  " + "-" * 72)
    for k in range(6, -7, -1):
        a = act_cnt.get(k, 0)
        e = exp.get(k, 0.0)
        sd = math.sqrt(var.get(k, 1e-18))
        z = (a - e) / sd if sd > 0 else 0.0
        p_one = 1 - norm_cdf(z) if z > 0 else norm_cdf(z)
        flag = ""
        if abs(k) == 1:
            flag = "  <<< 让平所在格"
        print(f"  {k:<8}{a:<10}{e:<12.1f}{a-e:<+12.1f}{(a/e if e else 0):<9.2f}{z:<+8.2f}{p_one:<9.4f}{flag}")

    # 让平合并格（竞彩线 ±1 的两个方向，取决于让球方向）
    for lab, ks in [("恰好赢 1 球(主-客=+1)", [1]), ("恰好输 1 球(主-客=-1)", [-1])]:
        a = sum(act_cnt.get(k, 0) for k in ks)
        e = sum(exp.get(k, 0.0) for k in ks)
        sd = math.sqrt(sum(var.get(k, 0.0) for k in ks))
        z = (a - e) / sd if sd > 0 else 0
        p_one = 1 - norm_cdf(z) if z > 0 else norm_cdf(z)
        print(f"\n  【{lab}】")
        print(f"    实际 {a} 场   λ期望 {e:.1f} 场   差 {a-e:+.1f}   倍数 {a/e:.2f}x   z={z:+.2f}   P={p_one:.4f}")

    # ─────────── [2b] 只测「让平真实可投」的子样本 ───────────
    print(f"\n[2b] 只测让平【可投】的子样本 —— λ 认为 P(±1) 落在 15%~35% 的场次")
    print(f"     （竞彩只在有明显热门时才开让球线，那才是让平真正会出现的场）")
    for lab, k_sign in [("margin=+1 (主队让1球的让平)", 1), ("margin=-1 (主队受让1球的让平)", -1)]:
        A = E = V = 0.0
        nn = 0
        for r in rows:
            lh, la = r["lambda_home"], r["lambda_away"]
            if not (lh and la and lh > 0 and la > 0):
                continue
            p = margin_dist(lh, la).get(k_sign, 0.0)
            if not (0.15 <= p <= 0.35):
                continue
            nn += 1
            A += 1 if (r["hg"] - r["ag"]) == k_sign else 0
            E += p
            V += p * (1 - p)
        if nn == 0:
            print(f"  {lab}: 子样本为空")
            continue
        sd = math.sqrt(V)
        z = (A - E) / sd
        p_one = 1 - norm_cdf(z) if z > 0 else norm_cdf(z)
        print(f"  {lab}")
        print(f"    n={nn}   实际 {int(A)}  λ期望 {E:.1f}   倍数 {A/E:.2f}x"
              f"   z={z:+.2f}   P={p_one:.4f}")

    # ─────────── [1b] 分期对比 ───────────
    print(f"\n[1b] 分期对比（9 月是我们实际在用的模型，看它和前几个月是否一致）")
    print(f"  {'期':<12}{'n':<7}{'prob命中':<11}{'prob Brier':<13}{'lam命中':<11}{'lam Brier':<13}{'LLM命中':<10}")
    print("  " + "-" * 74)
    periods = [("2026-06", "2026-06-01", "2026-07-01"), ("2026-08", "2026-08-01", "2026-09-01"),
               ("2026-09", "2026-09-01", "2026-10-01")]
    for name, d0, d1 in periods:
        sub = [r for r in rows if d0 <= str(r["match_date"])[:10] < d1]
        if not sub:
            continue
        nn = len(sub)
        hp = mp = hl = ml = hg_ = 0
        bp = bl = 0.0
        for r in sub:
            act = 0 if r["hg"] > r["ag"] else (1 if r["hg"] == r["ag"] else 2)
            pp = [r["win_home"], r["win_draw"], r["win_away"]]
            if all(v is not None for v in pp):
                s = sum(pp); pp = [v / s for v in pp]
                bp += brier([pp], act); hp += (pp.index(max(pp)) == act); mp += 1
            lh, la = r["lambda_home"], r["lambda_away"]
            if lh and la and lh > 0 and la > 0:
                md = margin_dist(lh, la)
                lp = [sum(p for k, p in md.items() if k > 0), md.get(0, 0.0),
                      sum(p for k, p in md.items() if k < 0)]
                bl += brier([lp], act); hl += (lp.index(max(lp)) == act); ml += 1
            if r["llm_win"]:
                hg_ += (r["llm_win"] == ["主胜", "平", "客胜"][act])
        print(f"  {name:<12}{nn:<7}{hp/mp if mp else 0:<11.1%}{bp/mp if mp else 0:<13.4f}"
              f"{hl/ml if ml else 0:<11.1%}{bl/ml if ml else 0:<13.4f}{(hg_/nn if nn else 0):<10.1%}")

    # ─────────── [3] 今天下注的两场，λ 怎么看 ───────────
    print(f"\n[3] 今天下注的两场，λ 网格给了什么")
    # (fixture_id, jc_no, 让平的含义, 对应的净胜球)
    watch = [(1494771, "周日018", "维京(主)让1球", "维京恰好赢1球", 1, 0.206),
             (1570401, "周日028", "主队受让1球", "皇家社会(客)恰好赢1球", -1, 0.219)]
    db = SessionLocal()
    try:
        for fid, jc, line, side, k, mkt in watch:
            r = db.execute(text("""SELECT home_name, away_name, lambda_home, lambda_away,
                                          actual_home_goals hg, actual_away_goals ag
                                   FROM predictions WHERE fixture_id=:f"""),
                           {"f": fid}).mappings().first()
            if not r:
                print(f"  {fid}: 无预测")
                continue
            lh, la = r["lambda_home"], r["lambda_away"]
            p = margin_dist(lh, la).get(k, 0.0)
            sc = f"{r['hg']}-{r['ag']}" if r["hg"] is not None else "未开赛"
            print(f"  {jc} {fid} {r['home_name']} vs {r['away_name']}  λ=({lh:.2f},{la:.2f})  比分 {sc}")
            print(f"       {line} -> {side} = 净胜球 {k:+d}")
            print(f"       λ 给 {p:.1%}   市场给 {mkt:.1%}   经验头(下注依据) 37.5%")
            print(f"       λ/市场 = {p/mkt:.2f}x    {'λ 不认为有水' if p <= mkt else 'λ 认为有水(背离腿)'}")
    finally:
        db.close()
    print()


if __name__ == "__main__":
    main()
