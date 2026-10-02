# -*- coding: utf-8 -*-
"""竞彩 vs 锐盘：定价梯度 + EV 分桶 ROI + 实盘对账。

回答一个问题：**「竞彩赔率 > 锐盘(Pinnacle)公平价」到底是不是真水？**

三段，按「能不能立刻读」排序：

  [1] 定价梯度     —— 不用等赛果，攒几天就能读。比的是同一时刻的两个价。
  [2] EV 分桶 ROI  —— 要赛果。样本量要求见下。
  [3] 实盘对账     —— 用户实际下过的腿里，EV>0 与 EV<0 各自的真实 ROI。

【判据】最高 EV 桶的 ROI 置信区间下界 > 全池基线（单关约 -11.5%）才算有信号。
⚠️ **点估计为正不算数**：赔率 ~3 时单注 SD ≈ 1.5，要分辨 10pp 的 ROI 差
需要 n ≈ 200-300 腿（约 3-4 周竞彩），不是 100。

【为什么 [1] 最重要】它比的是「同一时刻竞彩 vs 锐盘」，不涉及结果，所以只要
几十场就能看出竞彩是不是系统性偏。**梯度本身就是信号，EV>0 只是它的推论。**

用法:
    .venv/bin/python tools/edge_report.py            # 全部三段
    .venv/bin/python tools/edge_report.py --pricing  # 只出定价梯度
"""
import argparse
import math
import sys

sys.path.insert(0, ".")

from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402

FINISHED = ("FT", "AET", "PEN")

# 全池基线：无 edge 时的期望 ROI。单关 88.5% 返还 -> -11.5%
BASELINE_ROI = -0.115

# EV 分桶
BUCKETS = [(-9.9, 0.0, "<=0"), (0.0, 0.03, "0~3%"), (0.03, 0.06, "3~6%"),
           (0.06, 0.10, "6~10%"), (0.10, 9.9, ">10%")]

# 定价梯度按锐盘隐含概率分桶
PRICE_BUCKETS = [(0, .10), (.10, .20), (.20, .30), (.30, .40),
                 (.40, .50), (.50, .60), (.60, .80), (.80, 1.01)]


# ──── 取数 ────

def load_legs(since=None, before_kickoff=True):
    """每条腿取【开赛前最后一次】快照 —— 那才是最接近能下注的价。

    多快照（一天抓两次）是故意的：留着可以看赔率怎么动。但做 ROI 只能选一个价，
    选收盘前最近的那个。
    """
    db = SessionLocal()
    try:
        sql = """
            SELECT s.*, f.goals_home hg, f.goals_away ag, f.status_short st
            FROM odds_snapshots s
            JOIN (
                SELECT fixture_id, market, selection, MAX(fetched_at) mf
                FROM odds_snapshots
                WHERE fixture_id IS NOT NULL
                GROUP BY fixture_id, market, selection
            ) t ON s.fixture_id = t.fixture_id AND s.market = t.market
               AND s.selection = t.selection AND s.fetched_at = t.mf
            LEFT JOIN fixtures f ON f.id = s.fixture_id
            WHERE s.fixture_id IS NOT NULL
        """
        args = {}
        if before_kickoff:
            sql += " AND (s.kickoff_at IS NULL OR s.fetched_at <= s.kickoff_at)"
        if since:
            sql += " AND s.fetched_at >= :since"
            args["since"] = since
        sql += " ORDER BY s.fetched_at"
        return db.execute(text(sql), args).mappings().all()
    finally:
        db.close()


def leg_won(market, selection, line_val, hg, ag):
    """这条腿中没中。返回 True/False，判定不了返回 None。"""
    if hg is None or ag is None:
        return None
    m = hg - ag
    if market == "1x2":
        return {"主胜": m > 0, "平": m == 0, "客胜": m < 0}.get(selection)
    if market == "hhad":
        if line_val is None:
            return None
        a = m + float(line_val)          # 竞彩：让球线加在主队身上
        return {"让胜": a > 0, "让平": a == 0, "让负": a < 0}.get(selection)
    return None


# ──── 统计小工具 ────

def wilson(k, n, z=1.96):
    """命中的 Wilson 区间（比例用）。返回 (lo, hi)。"""
    if not n:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def roi_ci(payoffs, z=1.96):
    """ROI 的置信区间。

    ⚠️ ROI 是【非二元赔付】的均值，不是比例，所以**不能用 Wilson**（Wilson 只
    管比例）。这里用均值 +/- z*SE 的正态近似 —— 这也是钱的口径，判据用它。
    ⚠️ 正态近似在 n 小、赔付偏度大时偏乐观，所以表里始终同时列 n。
    """
    n = len(payoffs)
    if n < 2:
        return (float("nan"), float("nan"), float("nan"))
    mean = sum(payoffs) / n
    var = sum((x - mean) ** 2 for x in payoffs) / (n - 1)
    se = math.sqrt(var / n)
    return (mean, mean - z * se, mean + z * se)


def line(ch=104):
    print("-" * ch)


# ──── [1] 定价梯度 ────

def report_pricing(rows):
    print()
    print("[1] 定价梯度 —— 竞彩去水% 减 锐盘公平价%（按锐盘隐含概率分桶）")
    print("    不用等赛果。同一时刻两个价直接比。竞彩若系统性高估热门/低估冷门，")
    print("    这里会出现单调梯度 —— 那就是信号本身。")
    print()
    usable = [r for r in rows if r["pin_fair"] is not None and r["jc_prob"] is not None]
    if not usable:
        print("    没有配对数据（锐盘公平价为空）。先跑 tools/odds_watch.py 攒数据。")
        return
    print(f"    {'锐盘隐含%':<12}{'场次':<8}{'竞彩-锐盘(pp)':<16}{'竞彩/锐盘':<12}{'EV均值':<10}")
    line(66)
    for lo, hi in PRICE_BUCKETS:
        sub = [r for r in usable if lo <= float(r["pin_fair"]) < hi]
        if not sub:
            continue
        n = len(sub)
        dpp = sum(float(r["jc_prob"]) - float(r["pin_fair"]) for r in sub) / n * 100
        ratio = sum(float(r["jc_prob"]) for r in sub) / sum(float(r["pin_fair"]) for r in sub)
        ev = [float(r["ev"]) for r in sub if r["ev"] is not None]
        evm = sum(ev) / len(ev) if ev else float("nan")
        print(f"    {f'{lo:.0%}-{hi:.0%}':<12}{n:<8}{dpp:+.2f}{'':<11}{ratio:<12.3f}{evm*100:+.2f}%")
    print()
    print(f"    合计配对腿 {len(usable)} 条")


# ──── [2] EV 分桶 ROI ────

def report_buckets(rows):
    print()
    print("[2] EV 分桶事后 ROI（要赛果）")
    settled = []
    for r in rows:
        if r["ev"] is None or r["odds"] is None:
            continue
        if r["st"] not in FINISHED:
            continue
        won = leg_won(r["market"], r["selection"], r["line_val"], r["hg"], r["ag"])
        if won is None:
            continue
        settled.append((float(r["ev"]), float(r["odds"]), won))

    if not settled:
        print("    还没有已完赛且配了锐盘的腿。等比赛打完（backfill_pred 会回填赛果）。")
        return

    print(f"    {'EV 档':<10}{'n':<6}{'命中率':<18}{'ROI':<22}{'vs 基线':<10}")
    line(70)
    for lo, hi, lab in BUCKETS:
        sub = [x for x in settled if lo <= x[0] < hi]
        if not sub:
            continue
        n = len(sub)
        k = sum(1 for x in sub if x[2])
        pay = [x[1] - 1 if x[2] else -1.0 for x in sub]
        mean, clo, chi = roi_ci(pay)
        wlo, whi = wilson(k, n)
        roi_s = f"{mean:+.1%} [{clo:+.1%},{chi:+.1%}]" if n > 1 else "n 太小"
        vs = f"{mean - BASELINE_ROI:+.1%}" if n > 1 else "-"
        print(f"    {lab:<10}{n:<6}{k/n:.1%} [{wlo:.0%},{whi:.0%}]   {roi_s:<22}{vs}")
    allpay = [x[1] - 1 if x[2] else -1.0 for x in settled]
    amean, alo, ahi = roi_ci(allpay)
    print()
    print(f"    全池 {len(settled)} 腿  ROI {amean:+.1%} [{alo:+.1%},{ahi:+.1%}]"
          f"    基线 {BASELINE_ROI:+.1%}")
    print()
    print("    🔴 判据：某个 EV 桶的【ROI 置信下界】> 基线才算有信号。")
    print(f"       n 小的时候区间会宽到包含基线 —— 那是「还不知道」，不是「有效」。")
    n_need = int((1.5 / 0.10) ** 2)
    print(f"       要分辨 10pp 的 ROI 差，n 约需 {n_need} 腿（赔率~3 时单注 SD≈1.5）。")


# ──── [3] 实盘对账 ────

def report_live(rows):
    print()
    print("[3] 实盘对账 —— 用户真正下过的腿，按锐盘 EV 分组")
    snap = {}
    for r in rows:
        if r["fixture_id"] is None:
            continue
        mk = "handicap" if r["market"] == "hhad" else r["market"]
        key = (int(r["fixture_id"]), mk, r["selection"])
        if key not in snap and r["ev"] is not None:
            snap[key] = r

    db = SessionLocal()
    try:
        legs = db.execute(text("""
            SELECT l.fixture_id, l.market, l.selection, l.odds, l.leg_result,
                   l.actual_score, j.bet_date
            FROM bet_legs l JOIN bet_journal j ON j.id = l.ticket_id
            WHERE l.fixture_id IS NOT NULL
            ORDER BY j.bet_date
        """)).mappings().all()
    finally:
        db.close()

    hit, miss = [], []
    for lg in legs:
        r = snap.get((int(lg["fixture_id"]), lg["market"], lg["selection"]))
        if not r:
            continue
        ev = float(r["ev"])
        odds = float(lg["odds"])
        won = (lg["leg_result"] == "win")
        (hit if ev > 0 else miss).append((odds, won))

    if not hit and not miss:
        print("    没有可对账的腿（实盘腿还没和快照对上）。")
        return
    for lab, group in (("EV > 0", hit), ("EV <= 0", miss)):
        if not group:
            print(f"    {lab:<10} 无")
            continue
        n = len(group)
        k = sum(1 for _, w in group if w)
        pay = [o - 1 if w else -1.0 for o, w in group]
        mean, lo, hi = roi_ci(pay)
        print(f"    {lab:<10} n={n:<4} 命中 {k}/{n} = {k/n:.0%}   "
              f"ROI {mean:+.1%} [{lo:+.1%},{hi:+.1%}]")
    print()
    print("    ⚠️ 样本极小（实盘才 20 条腿），这一栏要攒很久才有意义。")


# ──── [4] 让球三路专项 ────

def report_hhad(rows):
    """让胜/让平/让负 各自的「实际 vs 锐盘公平价」。

    让平是不是被低估 —— 这是问了两周的问题。这里给的是**独立于 λ 头**的第三把尺子：
    锐盘当场就报了它认为的公平价，赛果出来一比就知道锐盘报没报错。
    """
    print()
    print("[4] 让球三路专项 —— 实际命中 vs 锐盘公平价（独立于 λ 头的第三把尺子）")
    sub = [r for r in rows if r["market"] == "hhad" and r["pin_fair"] is not None]
    if not sub:
        print("    没有配上锐盘的让球腿。")
        return
    print()
    print(f"    {'选项':<8}{'n':<5}{'实际命中':<16}{'竞彩%':<10}{'锐盘%':<10}"
          f"{'实际/锐盘':<12}{'平均EV':<10}")
    line(76)
    for sel in ("让胜", "让平", "让负"):
        g = [r for r in sub if r["selection"] == sel]
        if not g:
            continue
        n = len(g)
        won = 0
        for r in g:
            w = leg_won(r["market"], r["selection"], r["line_val"], r["hg"], r["ag"])
            won += 1 if w else 0
        act = won / n
        jc = sum(float(r["jc_prob"]) for r in g if r["jc_prob"] is not None) / n
        pin = sum(float(r["pin_fair"]) for r in g) / n
        ev = sum(float(r["ev"]) for r in g if r["ev"] is not None) / n
        print(f"    {sel:<8}{n:<5}{f'{won}/{n} = {act:.1%}':<16}{jc:<10.1%}{pin:<10.1%}"
              f"{act/pin if pin else 0:<12.2f}{ev*100:+.1f}%")
    print()
    print("    「实际/锐盘」≈1 说明锐盘当初就报对了，那一路没有可捡的便宜；")
    print("    「锐盘%」明显高于「竞彩%」才是竞彩给多了（EV 才会为正）。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="只算该时间之后的快照 (YYYY-MM-DD)")
    ap.add_argument("--pricing", action="store_true", help="只出定价梯度")
    args = ap.parse_args()

    rows = load_legs(since=args.since)
    print("=" * 104)
    print(f"== 竞彩 vs 锐盘 复盘   配对腿 {len(rows)} 条"
          + (f"   since {args.since}" if args.since else ""))
    print("=" * 104)
    if not rows:
        print("\n  表里还没有配对数据。先跑 tools/odds_watch.py 抓几轮。\n")
        return

    report_pricing(rows)
    if not args.pricing:
        report_buckets(rows)
        report_hhad(rows)
        report_live(rows)
    print()


if __name__ == "__main__":
    main()
