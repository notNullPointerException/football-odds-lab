# -*- coding: utf-8 -*-
"""Pinnacle 锚判据的测量仪：体彩每一条腿，相对 Pinnacle 公平价是不是【卖便宜了】。

【判据原理】体彩抽水是定额比例，赔率 = 0.8705 / p_体彩。押一条腿的 EV：
    ROI = P真 x 赔率 - 1
如果用 Pinnacle 的去水概率当 P真（锐盘是最接近真值的估计），则
    ROI = 0.8705 x (pin_fair / jc_prob) - 1
=> 【开口线】pin_fair / jc_prob >= 1/0.8705 = 1.1488   （+14.9% 相对）
   这个 1.1488 是硬常数，见 [[football-main-pick-discipline]] 2026-10-07 节。

🔴 本脚本【只测量，不下注建议】。它回答「哪条腿的价差够得着开口线」，
   **不等价于「这条腿会赢」** —— 价差是 edge 的【输入】，不是 edge 本身。
   要确认还得等赛果（见 [3] 节），而赛果积累比价差慢得多。

【必须去重】odds_snapshots 的 uniq_key 带「抓取分钟」，一天两跑 = 两套行并存不覆盖。
样本单位是【比赛 x 市场 x 选择】，不是快照行。直接 AVG() 会把跑了两遍的场加权两次。

用法:
  .venv/bin/python tools/pin_anchor_check.py            # 全量读数
  .venv/bin/python tools/pin_anchor_check.py --need-n   # 只算需要多少场才判得了
"""
import math
import sys
import unicodedata

sys.path.insert(0, ".")

from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402

BAR = 1.0 / 0.8705          # 1.1488 开口线
PAYBACK = 0.8705


def wdisp(s):
    return sum(2 if unicodedata.east_asian_width(c) in "FW" else 1 for c in str(s))


def pad(s, w):
    return str(s) + " " * max(0, w - wdisp(s))


def rpad(s, w):
    return " " * max(0, w - wdisp(s)) + str(s)


def wilson(h, n, z=1.96):
    if not n:
        return 0.0, 0.0
    p = h / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5
    return (c - m) / d, (c + m) / d


def load():
    """去重后的快照腿 + 该场的赛果（若已出）。"""
    db = SessionLocal()
    try:
        return db.execute(text("""
            SELECT s.fixture_id, s.market, s.selection, s.line_val,
                   s.jc_prob, s.pin_fair, s.odds,
                   f.goals_home, f.goals_away, f.fulltime_home, f.fulltime_away,
                   f.status_short, s.match_label
            FROM odds_snapshots s
            JOIN (SELECT fixture_id, market, selection, MAX(fetched_at) mx
                  FROM odds_snapshots WHERE fixture_id IS NOT NULL
                  GROUP BY fixture_id, market, selection) d
              ON d.fixture_id = s.fixture_id AND d.market = s.market
             AND d.selection = s.selection AND d.mx = s.fetched_at
            LEFT JOIN fixtures f ON f.id = s.fixture_id
            WHERE s.fixture_id IS NOT NULL AND s.jc_prob IS NOT NULL
            ORDER BY s.market, s.selection, s.fixture_id
        """)).mappings().all()
    finally:
        db.close()


def actual_leg(row):
    """这条腿实际中没中。返回 1/0/None(未出结果)。"""
    hg = row["fulltime_home"] if row["fulltime_home"] is not None else row["goals_home"]
    ag = row["fulltime_away"] if row["fulltime_away"] is not None else row["goals_away"]
    if hg is None or ag is None:
        return None
    sel = row["selection"]
    if row["market"] == "1x2":
        want = {"主胜": "H", "平": "D", "客胜": "A"}[sel]
        got = "H" if hg > ag else ("A" if hg < ag else "D")
        return 1 if want == got else 0
    ln = float(row["line_val"])
    adj = (hg - ag) + ln
    want = {"让胜": 1, "让平": 0, "让负": -1}[sel]
    got = 1 if adj > 0 else (-1 if adj < 0 else 0)
    return 1 if want == got else 0


def need_n(p_market, rel=BAR, power=0.80, alpha=0.05):
    """要在 80% 功效 / 5% 显著性下，把「相对偏差 = rel」检出来，需要多少条腿。"""
    z_a, z_b = 1.959964, 0.8416212
    p1 = p_market * rel
    d = p1 - p_market
    if d <= 0:
        return None
    return math.ceil((z_a * math.sqrt(p_market * (1 - p_market))
                      + z_b * math.sqrt(p1 * (1 - p1))) ** 2 / d ** 2)


def main():
    rows = load()
    print("=" * 100)
    print(f"Pinnacle 锚读数（已去重：每 fixture+market+selection 取最新快照）   开口线 = {BAR:.4f}")
    print("=" * 100)

    groups = {}
    for r in rows:
        key = (r["market"], r["selection"], str(r["line_val"]))
        groups.setdefault(key, []).append(r)

    print(f"\n[1] 定价差（只看价，不看赛果）")
    print("  " + pad("市场", 6) + pad("选择", 7) + rpad("线", 6) + rpad("场数", 6)
          + rpad("体彩p", 9) + rpad("锐盘p", 9) + rpad("比值", 8) + rpad("若为真ROI", 11)
          + "够线?")
    print("  " + "-" * 92)
    hits = []
    for (mkt, sel, ln), g in sorted(groups.items()):
        n = len(g)
        jc = sum(float(x["jc_prob"]) for x in g) / n
        pin = sum(float(x["pin_fair"]) for x in g if x["pin_fair"] is not None)
        npx = sum(1 for x in g if x["pin_fair"] is not None)
        if not npx:
            continue
        pin /= npx
        ratio = pin / jc
        roi = PAYBACK * ratio - 1
        flag = "YES <--" if ratio >= BAR else ""
        if ratio >= BAR:
            hits.append((mkt, sel, ln, n, jc, pin, ratio, roi))
        print("  " + pad(mkt, 6) + pad(sel, 7) + rpad(ln, 6) + rpad(str(n), 6)
              + rpad(f"{jc:.4f}", 9) + rpad(f"{pin:.4f}", 9) + rpad(f"{ratio:.4f}", 8)
              + rpad(f"{roi*100:+.1f}%", 11) + flag)

    print(f"\n  ==> 够线的腿: {len(hits)} 条" + ("" if hits else "（一条都没有）"))

    print(f"\n[2] 赛果侧（价差够线还不够，要赛果确认）")
    res = [r for r in rows if actual_leg(r) is not None]
    print(f"  有赛果的腿: {len(res)} 条")
    if res:
        for (mkt, sel, ln), _ in sorted(groups.items()):
            g = [r for r in res if (r["market"], r["selection"], str(r["line_val"]))
                 == (mkt, sel, ln)]
            if not g:
                continue
            hit = sum(actual_leg(r) for r in g)
            lo, hi = wilson(hit, len(g))
            print(f"  {pad(mkt,6)}{pad(sel,7)}{rpad(ln,6)}n={len(g):<4} "
                  f"实际 {hit}/{len(g)} = {hit/len(g):.3f}  CI[{lo:.3f},{hi:.3f}]")
    else:
        print("  ⚠️ 一条都还没有 —— 快照里的场尚未开赛，无法算实际命中率。")
        print("     ⇒ 现在只能读【定价差】。价差是 edge 的输入，不是 edge 本身。")

    print(f"\n[3] 要多少样本才判得了（80% 功效 / 5% 显著性 / 检出相对偏差 {BAR:.4f}）")
    print("  " + pad("市场", 6) + pad("选择", 7) + rpad("线", 6) + rpad("体彩p", 8)
          + rpad("需腿数", 8) + rpad("已有赛果", 9) + "进度")
    print("  " + "-" * 58)
    for (mkt, sel, ln), g in sorted(groups.items()):
        n = len(g)
        jc = sum(float(x["jc_prob"]) for x in g) / n
        need = need_n(jc)
        cur = len([r for r in g if actual_leg(r) is not None])
        print("  " + pad(mkt, 6) + pad(sel, 7) + rpad(ln, 6) + rpad(f"{jc:.4f}", 8)
              + rpad(f"{need}", 8) + rpad(f"{cur}", 9)
              + f"{cur/need*100:.1f}%")

    print(f"\n[4] 现状小结")
    n_games = len({r['fixture_id'] for r in rows})
    print(f"  去重后 {n_games} 场 / {len(rows)} 条腿；有赛果 {len(res)} 条")
    n_1x2 = need_n(0.4787)
    n_hh = need_n(0.3019)
    print(f"  1x2 路（每场必出 1 条）: 需 {n_1x2} 条 ≈ {n_1x2//40}-{n_1x2//20} 天")
    print(f"  hhad 路（只有该线型的场才出）: 需 {n_hh} 条；若 40% 的场是 -1 线、"
          f"每天 25 场 → {n_hh//10} 天")
    print("  ⚠️ 两件事别搞混：")
    print("     ① 价差 = edge 的【输入】，不是 edge 本身 —— 现在 0 条有赛果，无法确认。")
    print("     ② 单时点读出的比值可能来自【同一条 stale 线被反复快照】，不是真价差。")


if __name__ == "__main__":
    main()
