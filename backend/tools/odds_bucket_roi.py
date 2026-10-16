# -*- coding: utf-8 -*-
"""体彩 1x2 按「去水概率档」算实际命中率与 ROI —— 回答「有没有任何一格是正 EV」。

【为什么要跑这个】
2026-10-07 复盘：下注判据「≥2 个模型头 prob>1/赔率」被实测证伪（91% 恒真、
edge 与结果零分离，见 [[football-main-pick-discipline]]）。判据要换成什么，
取决于【体彩这个市场本身有没有可捡的错价】—— 如果市场是有效的，换什么门槛都没用。

【为什么是一维不是二维】
体彩抽水是**定额比例**（赔率 = 返还率 / 概率），所以 odds 与去水 p 是同一信息的
两种写法，(p档 × 赔率档) 二维网格**退化**。真正的一维判据是：

    ROI(某腿) = P(真) x 赔率 - 1 = 返还率 x P(真)/P(市场) - 1 = 0.8705 x (P真/P市) - 1
    打平需要 P真 比 P市 高 14.9%（相对）

所以本脚本查的是：**各概率档的相对偏差，有没有哪一档够得着 +14.9%。**

【数据】sfc_issue_matches：体彩 had_h/d/a + 彩果，18,890 行可用。
彩果编码 3=主胜 1=平 0=客胜（已用三路赔率交叉验证）。

⚠️ 统计口径：一场 3 条腿**高度相关**（同一场只出一个结果），不是 3 个独立观测。
所以 ROI 的置信区间走 **按场聚类 bootstrap**，不是把腿当独立样本。
命中率那列仍给 Wilson（腿级），但它会偏窄，仅供参考。

用法: .venv/bin/python tools/odds_bucket_roi.py
"""
import random
import sys
import unicodedata

sys.path.insert(0, ".")

from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402

BUCKETS = [(0.00, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.45),
           (0.45, 0.55), (0.55, 0.65), (0.65, 0.80), (0.80, 1.01)]
PAYBACK_MIN, PAYBACK_MAX = 0.80, 0.95   # 挡掉抓坏的赔率行
N_BOOT = 2000
SEED = 20261007


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
    db = SessionLocal()
    try:
        return db.execute(text("""
            SELECT issue, had_h, had_d, had_a, cz_result
            FROM sfc_issue_matches
            WHERE had_h IS NOT NULL AND cz_result IS NOT NULL AND cz_result <> '*'
              AND had_h > 1 AND had_d > 1 AND had_a > 1
            ORDER BY issue, no
        """)).mappings().all()
    finally:
        db.close()


def prep(rows):
    """-> [(issue, [(p, odds, hit), ...3], p_max, spread)]，剔掉赔率异常的场。"""
    games = []
    for r in rows:
        o = [float(r["had_h"]), float(r["had_d"]), float(r["had_a"])]
        inv = [1 / x for x in o]
        s = sum(inv)
        pb = 1 / s
        if not (PAYBACK_MIN <= pb <= PAYBACK_MAX):
            continue
        # 彩果 3=主胜 1=平 0=客胜
        act = {"3": 0, "1": 1, "0": 2}.get(str(r["cz_result"]))
        if act is None:
            continue
        legs = [((inv[i] / s), o[i], 1 if i == act else 0) for i in range(3)]
        pmax = max(l[0] for l in legs)
        spread = max(o) / min(o)          # 三路赔率离散度
        games.append((r["issue"], legs, pmax, spread))
    return games


def main():
    games = prep(load())
    n_g = len(games)
    legs = [l for _, ls, _, _ in games for l in ls]
    print("=" * 96)
    print(f"体彩 1x2 概率档 -> 实际命中率 / ROI      可用 {n_g} 场 ({len(legs)} 条腿)")
    print("=" * 96)

    print("\n[1] 全部腿：按去水概率档（正 EV 需要 ROI > 0；打平需 P真/P市 >= 1.149）")
    hdr = (pad("p档", 14) + rpad("腿数", 8) + rpad("理论p", 9) + rpad("实际", 9)
           + rpad("相对", 9) + rpad("均赔", 8) + rpad("ROI", 10) + "Wilson 95%CI")
    print("  " + hdr)
    print("  " + "-" * 92)
    for lo, hi in BUCKETS:
        sel = [l for l in legs if lo <= l[0] < hi]
        if len(sel) < 30:
            print("  " + pad(f"{lo:.2f}-{hi:.2f}", 14) + rpad(str(len(sel)), 8) + "  (样本不足)")
            continue
        n = len(sel)
        th = sum(l[0] for l in sel) / n
        ac = sum(l[2] for l in sel) / n
        od = sum(l[1] for l in sel) / n
        roi = sum(l[1] * l[2] for l in sel) / n - 1
        wlo, whi = wilson(sum(l[2] for l in sel), n)
        print("  " + pad(f"{lo:.2f}-{hi:.2f}", 14) + rpad(str(n), 8)
              + rpad(f"{th:.4f}", 9) + rpad(f"{ac:.4f}", 9)
              + rpad(f"{ac/th:.3f}", 9) + rpad(f"{od:.2f}", 8)
              + rpad(f"{roi*100:+.1f}%", 10)
              + f"[{wlo*100:.1f},{whi*100:.1f}]")

    print("\n[2] 每场只买【市场热门】(p 最大那路) —— 按场聚类 bootstrap")
    fav = []
    for issue, ls, _, _ in games:
        i = max(range(3), key=lambda k: ls[k][0])
        fav.append((issue, ls, None, None, i))
    per = [ls[i][1] * ls[i][2] - 1.0 for _, ls, _, _, i in fav]
    hit = sum(ls[i][2] for _, ls, _, _, i in fav)
    n = len(per)
    rng = random.Random(SEED)
    bs = sorted(sum(per[rng.randrange(n)] for _ in range(n)) / n for _ in range(N_BOOT))
    print(f"  n={n}  命中 {hit} = {hit/n*100:.1f}%   ROI {sum(per)/n*100:+.2f}%"
          f"   95%CI(按场聚类) [{bs[int(.025*N_BOOT)]*100:+.2f}%, {bs[int(.975*N_BOOT)]*100:+.2f}%]")

    print("\n[3] 每场只买【冷门】(p 最小那路) —— 对照，看有没有反向错价")
    per = []
    for _, ls, _, _ in games:
        i = min(range(3), key=lambda k: ls[k][0])
        per.append(ls[i][1] * ls[i][2] - 1.0)
    bs = sorted(sum(per[rng.randrange(n)] for _ in range(n)) / n for _ in range(N_BOOT))
    print(f"  n={n}  ROI {sum(per)/n*100:+.2f}%"
          f"   95%CI(按场聚类) [{bs[int(.025*N_BOOT)]*100:+.2f}%, {bs[int(.975*N_BOOT)]*100:+.2f}%]")

    print("\n[4] 市场是否校准：各档「实际 - 理论」，相对偏差")
    print("  " + pad("p档", 14) + rpad("腿数", 8) + rpad("实际", 9) + rpad("理论", 9) + "差(pp)")
    print("  " + "-" * 60)
    for lo, hi in BUCKETS:
        sel = [l for l in legs if lo <= l[0] < hi]
        if len(sel) < 30:
            continue
        n = len(sel)
        th = sum(l[0] for l in sel) / n
        ac = sum(l[2] for l in sel) / n
        print("  " + pad(f"{lo:.2f}-{hi:.2f}", 14) + rpad(str(n), 8)
              + rpad(f"{ac:.4f}", 9) + rpad(f"{th:.4f}", 9) + f"{(ac-th)*100:+.2f}")

    print("\n[5] 冷门率 vs 三路赔率离散度（分散=难猜，理论上应该对应更多冷门）")
    print("  " + pad("max/min", 12) + rpad("场数", 7) + rpad("热门命中率", 12) + "Wilson 95%CI")
    print("  " + "-" * 62)
    for lo, hi in [(1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 6.0), (6.0, 99.0)]:
        sel = [g for g in games if lo <= g[3] < hi]
        if len(sel) < 30:
            continue
        n = len(sel)
        # 该场热门腿是否命中
        h = sum(1 for _, ls, _, _ in sel
                for k in [max(range(3), key=lambda k: ls[k][0])] if ls[k][2] == 1)
        wlo, whi = wilson(h, n)
        print("  " + pad(f"{lo:.1f}-{hi:.1f}", 12) + rpad(str(n), 7)
              + rpad(f"{h/n*100:.1f}%", 12) + f"[{wlo*100:.1f},{whi*100:.1f}]")

    print("\n  注: ROI 打平线在 0；体彩抽水地板 = 返还率-1 = -12.95%。")
    print("      任何一档 ROI 的 CI 上界 < 0 即该档无错价。")


if __name__ == "__main__":
    main()


def halves_and_best_cell():
    """两半同号 + 最优格 CI —— 项目判据的两道关。"""
    games = prep(load())
    h = len(games) // 2
    legs_a = [l for g in games[:h] for l in g[1]]
    legs_b = [l for g in games[h:] for l in g[1]]
    print("\n[6] 两半同号检验（按 issue 顺序切半）—— 相对偏差 P真/P市")
    print("  " + pad("p档", 14) + rpad("前半天", 20) + "后半天")
    print("  " + "-" * 60)
    for lo, hi in BUCKETS:
        row = []
        for legs in (legs_a, legs_b):
            sel = [l for l in legs if lo <= l[0] < hi]
            if len(sel) < 20:
                row.append("n 不足")
                continue
            n = len(sel)
            th = sum(l[0] for l in sel) / n
            ac = sum(l[2] for l in sel) / n
            row.append(f"n={n:<5} {ac/th:.3f}")
        print("  " + pad(f"{lo:.2f}-{hi:.2f}", 14)
              + rpad(row[0], 20) + row[1])

    print("\n[7] 最优格 = 0.65-0.80，按场聚类 bootstrap 看 ROI 的 CI 上界")
    sel_g = [g for g in games
             if any(0.65 <= l[0] < 0.80 for l in g[1])]
    # 只买该格里 p 最大的那条腿，每场最多一条
    per = []
    for _, ls, _, _ in sel_g:
        cand = [l for l in ls if 0.65 <= l[0] < 0.80]
        if not cand:
            continue
        lg = max(cand, key=lambda x: x[0])
        per.append(lg[1] * lg[2] - 1.0)
    n = len(per)
    rng = random.Random(SEED)
    bs = sorted(sum(per[rng.randrange(n)] for _ in range(n)) / n for _ in range(N_BOOT))
    print(f"  n={n}  ROI {sum(per)/n*100:+.2f}%   "
          f"95%CI [{bs[int(.025*N_BOOT)]*100:+.2f}%, {bs[int(.975*N_BOOT)]*100:+.2f}%]")
    print(f"  打平需要 +0.00%；CI 上界 {'< 0 -> 该格无错价' if bs[int(.975*N_BOOT)] < 0 else '>= 0 -> 存疑，需再验'}")

    print("\n[8] 打平门槛 vs 唯一活线索的量级")
    print(f"  体彩 1x2 打平需要 P真/P市 >= 1/{0.8705:.4f} = {1/0.8705:.4f}  (+{(1/0.8705-1)*100:.1f}% 相对)")
    for rel in (1.05, 1.12, 1.149, 1.20, 1.48):
        print(f"    若某腿真实相对优势 {rel:.3f} -> ROI = 0.8705*{rel:.3f}-1 = {(0.8705*rel-1)*100:+.1f}%")


def shape_residual():
    """C 的非循环版本：控制「比赛形状」后，某路价格的相对异常还有没有预测力。

    做法：按 pmax(favourite 去水概率) 分 5 箱控制形状 ->
    箱内对每路的 log(p) 求均值 -> 残差 = log(p) - 箱内均值。
    残差 < 0 = 该路相对这个形状【异常短】(市场在防守这路)。
    然后看：残差档的实际命中率，有没有偏离市场自己给的概率。
    """
    import math
    games = prep(load())
    SHAPE = [(0.0, 0.40), (0.40, 0.50), (0.50, 0.62), (0.62, 0.75), (0.75, 1.01)]
    NAME = ["p_fav<0.40", "0.40-0.50", "0.50-0.62", "0.62-0.75", ">0.75"]

    # 每场 -> 形状箱
    shaped = []
    for _, ls, pmax, _ in games:
        for si, (lo, hi) in enumerate(SHAPE):
            if lo <= pmax < hi:
                shaped.append((si, ls))
                break

    print("\n" + "=" * 96)
    print("[9] C 非循环检验：控制形状后，某路价格异常短/长 -> 还有没有信息")
    print("=" * 96)
    legname = ["主胜", "平局", "客胜"]
    for li in range(3):
        # 箱内均值
        mus = {}
        for si in range(len(SHAPE)):
            v = [math.log(ls[li][0]) for s, ls in shaped if s == si]
            if v:
                mus[si] = sum(v) / len(v)
        print(f"\n  --- {legname[li]} ---")
        print("  " + pad("残差分档", 13) + rpad("腿数", 8) + rpad("市场p", 9)
              + rpad("实际", 9) + rpad("相对", 8) + rpad("均赔", 8) + "ROI")
        print("  " + "-" * 74)
        recs = [(math.log(ls[li][0]) - mus[s], ls[li]) for s, ls in shaped if s in mus]
        recs.sort(key=lambda x: x[0])
        n = len(recs)
        for qi, lab in enumerate(["Q1 最异常短", "Q2", "Q3", "Q4", "Q5 最异常长"]):
            chunk = recs[int(qi * n / 5):int((qi + 1) * n / 5)]
            if len(chunk) < 50:
                continue
            m = len(chunk)
            th = sum(c[1][0] for c in chunk) / m
            ac = sum(c[1][2] for c in chunk) / m
            od = sum(c[1][1] for c in chunk) / m
            roi = sum(c[1][1] * c[1][2] for c in chunk) / m - 1
            print("  " + pad(lab, 13) + rpad(str(m), 8) + rpad(f"{th:.4f}", 9)
                  + rpad(f"{ac:.4f}", 9) + rpad(f"{ac/th:.3f}", 8)
                  + rpad(f"{od:.2f}", 8) + f"{roi*100:+.1f}%")

    print("\n  [10] 只看【平局】：每场一条腿，按 平局残差 分 5 档的 ROI 与 CI")
    recs = []
    for s, ls in shaped:
        if s not in mus:
            continue
        recs.append((math.log(ls[1][0]) - mus[s], ls[1]))
    recs.sort(key=lambda x: x[0])
    n = len(recs)
    rng = random.Random(SEED)
    for qi, lab in enumerate(["Q1 平赔异常短", "Q2", "Q3", "Q4", "Q5 平赔异常长"]):
        chunk = recs[int(qi * n / 5):int((qi + 1) * n / 5)]
        per = [c[1][1] * c[1][2] - 1.0 for c in chunk]
        m = len(per)
        bs = sorted(sum(per[rng.randrange(m)] for _ in range(m)) / m for _ in range(N_BOOT))
        hit = sum(c[1][2] for c in chunk)
        th = sum(c[1][0] for c in chunk) / m
        print(f"  {pad(lab, 16)} n={m:<6} 市场p={th:.3f} 实际={hit/m:.3f} "
              f"ROI {sum(per)/m*100:+.1f}%  CI [{bs[int(.025*N_BOOT)]*100:+.1f}%, {bs[int(.975*N_BOOT)]*100:+.1f}%]")
