# -*- coding: utf-8 -*-
"""开口条件 -> 命中率扫描：回答「叫赢就赢 / 叫平就平 / 叫冷就冷」能到什么程度。

只扫历史 predictions 表（与 backtest_heads.py 同源），不动任何模型/阈值。
输出口径：每个「开口条件」（哪个头、置信门槛、要不要多头一致）下，
    633 场里叫了多少场、叫了中多少场、95% Wilson 置信区间。

用法: .venv/bin/python tools/call_rate.py
"""
import math
import sys
import unicodedata

sys.path.insert(0, ".")

from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402

MAXG = 11
LAB = ["主胜", "平", "客胜"]


def wdisp(s):
    return sum(2 if unicodedata.east_asian_width(c) in "FW" else 1 for c in str(s))


def pad(s, w):
    return str(s) + " " * max(0, w - wdisp(s))


def rpad(s, w):
    return " " * max(0, w - wdisp(s)) + str(s)


def pois(k, l):
    return math.exp(-l) * l ** k / math.factorial(k)


def margin_1x2(lh, la):
    d = {}
    for i in range(MAXG):
        for j in range(MAXG):
            p = pois(i, lh) * pois(j, la)
            d[i - j] = d.get(i - j, 0.0) + p
    s = sum(d.values())
    return [sum(v for k, v in d.items() if k > 0) / s,
            d.get(0, 0.0) / s,
            sum(v for k, v in d.items() if k < 0) / s]


def wilson(h, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = h / n
    dd = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / dd, (c + m) / dd


def load():
    db = SessionLocal()
    try:
        return db.execute(text("""SELECT win_home, win_draw, win_away,
            lambda_home, lambda_away, llm_win,
            actual_home_goals hg, actual_away_goals ag
            FROM predictions
            WHERE actual_home_goals IS NOT NULL AND actual_away_goals IS NOT NULL
              AND lambda_home IS NOT NULL AND lambda_away IS NOT NULL
            ORDER BY match_date""")).mappings().all()
    finally:
        db.close()


def prep(rows):
    out = []
    for r in rows:
        act = 0 if r["hg"] > r["ag"] else (1 if r["hg"] == r["ag"] else 2)
        pp = [r["win_home"], r["win_draw"], r["win_away"]]
        if all(v is not None for v in pp):
            s = sum(pp)
            pp = [v / s for v in pp]
        else:
            pp = None
        lh, la = r["lambda_home"], r["lambda_away"]
        lp = margin_1x2(lh, la) if (lh and la and lh > 0 and la > 0) else None
        llm = LAB.index(r["llm_win"]) if r["llm_win"] in LAB else None
        out.append((act, pp, lp, llm))
    return out


def line(args, fmt):
    print("  " + "".join(fmt % a for a in args))


def main():
    rows = prep(load())
    n = len(rows)
    cnt = [sum(1 for r in rows if r[0] == i) for i in range(3)]
    freq = [c / n for c in cnt]

    print("=" * 104)
    print(f"== 开口条件 -> 命中率   全历史 {n} 场   "
          f"实际分布 主{cnt[0]} 平{cnt[1]} 客{cnt[2]}")
    print("=" * 104)

    # ─────────── [0] 基础：无门槛 argmax ───────────
    print("\n[0] 无门槛：每次都叫自己首选方向（argmax），命中率")
    hdr = pad("头", 12) + rpad("叫了", 7) + rpad("中了", 7) + rpad("命中率", 9) + "95%CI"
    print("  " + hdr)
    print("  " + "-" * 60)
    for name, get_p in [("prob首选", lambda r: r[1]), ("lam首选", lambda r: r[2])]:
        sub = [r for r in rows if get_p(r)]
        m = len(sub)
        hit = sum(1 for r in sub if get_p(r).index(max(get_p(r))) == r[0])
        lo, hi = wilson(hit, m)
        print(pad(name, 12) + rpad(str(m), 7) + rpad(str(hit), 7)
              + rpad("%.1f%%" % (hit / m * 100), 9) + "[%.1f%%, %.1f%%]" % (lo * 100, hi * 100))
    sub = [r for r in rows if r[3] is not None]
    hit = sum(1 for r in sub if r[3] == r[0])
    lo, hi = wilson(hit, len(sub))
    print(pad("LLM首选", 12) + rpad(str(len(sub)), 7) + rpad(str(hit), 7)
          + rpad("%.1f%%" % (hit / len(sub) * 100), 9) + "[%.1f%%, %.1f%%]" % (lo * 100, hi * 100))
    print(pad("全叫主胜", 12) + rpad(str(n), 7) + rpad(str(cnt[0]), 7)
          + rpad("%.1f%%" % (freq[0] * 100), 9) + "(无脑基准)")

    # ─────────── [1] 单头置信门槛扫描 ───────────
    print("\n[1] 单头置信门槛：max(P) >= T 才开口（T 越高开口越少）")
    print("  " + pad("T", 6) + pad("prob叫/中", 13) + pad("prob命中", 11) + pad("prob 95%CI", 17)
          + pad("lam叫/中", 12) + pad("lam命中", 10) + "lam 95%CI")
    print("  " + "-" * 100)
    for T in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]:
        cells = []
        for get_p in [lambda r: r[1], lambda r: r[2]]:
            sub = [r for r in rows if get_p(r) and max(get_p(r)) >= T]
            hit = sum(1 for r in sub if get_p(r).index(max(get_p(r))) == r[0])
            m = len(sub)
            lo, hi = wilson(hit, m)
            cells.append((m, hit, lo, hi))
        a, b = cells
        print("  " + rpad("%.2f" % T, 6)
              + rpad("%d/%d" % (a[0], a[1]), 13) + rpad("%.1f%%" % (a[1] / a[0] * 100 if a[0] else 0), 11)
              + rpad("[%.1f,%.1f]" % (a[2] * 100, a[3] * 100), 17)
              + rpad("%d/%d" % (b[0], b[1]), 12)
              + rpad("%.1f%%" % (b[1] / b[0] * 100 if b[0] else 0), 10)
              + "[%.1f,%.1f]" % (b[2] * 100, b[3] * 100))

    # ─────────── [2] 双头一致 ───────────
    print("\n[2] 双头一致才开口（prob 与 lam 首选同方向），可再叠加置信门槛")
    print("  " + pad("条件", 26) + rpad("叫了", 7) + rpad("中了", 7) + rpad("命中率", 9) + "95%CI")
    print("  " + "-" * 72)
    for T in [0.0, 0.40, 0.45, 0.50]:
        sub = [r for r in rows if r[1] and r[2]
               and r[1].index(max(r[1])) == r[2].index(max(r[2]))
               and max(r[1]) >= T and max(r[2]) >= T]
        hit = sum(1 for r in sub if r[1].index(max(r[1])) == r[0])
        m = len(sub)
        lo, hi = wilson(hit, m)
        lab = "双头一致" if T == 0 else "双头一致+双方>=%.2f" % T
        print("  " + pad(lab, 26) + rpad(str(m), 7) + rpad(str(hit), 7)
              + rpad("%.1f%%" % (hit / m * 100 if m else 0), 9) + "[%.1f,%.1f]" % (lo * 100, hi * 100))

    # ─────────── [3] 三头一致 ───────────
    print("\n[3] 三头一致（prob+lam+LLM 同方向）")
    for T in [0.0, 0.45]:
        sub = [r for r in rows if r[1] and r[2] and r[3] is not None
               and r[1].index(max(r[1])) == r[2].index(max(r[2])) == r[3]
               and max(r[1]) >= T and max(r[2]) >= T]
        hit = sum(1 for r in sub if r[3] == r[0])
        m = len(sub)
        lo, hi = wilson(hit, m)
        lab = "三头一致" if T == 0 else "三头一致+双置信>=0.45"
        print("  " + pad(lab, 26) + rpad(str(m), 7) + rpad(str(hit), 7)
              + rpad("%.1f%%" % (hit / m * 100 if m else 0), 9) + "[%.1f,%.1f]" % (lo * 100, hi * 100))

    # ─────────── [4] 叫平就平 ───────────
    print("\n[4] 叫平就平：平局置信 >= T 才叫平（平局自然频率 %.1f%%）" % (freq[1] * 100))
    print("  " + pad("T(prob平)", 12) + pad("lam叫/中", 13) + pad("lam命中", 11)
          + pad("prob叫/中", 13) + pad("prob命中", 11))
    print("  " + "-" * 72)
    for T in [0.20, 0.25, 0.30, 0.35]:
        cells = []
        for get_p in [lambda r: r[2], lambda r: r[1]]:
            sub = [r for r in rows if get_p(r) and get_p(r)[1] >= T]
            hit = sum(1 for r in sub if r[0] == 1)
            cells.append((len(sub), hit))
        a, b = cells
        print("  " + rpad("%.2f" % T, 12)
              + rpad("%d/%d" % (a[0], a[1]), 13) + rpad("%.1f%%" % (a[1] / a[0] * 100 if a[0] else 0), 11)
              + rpad("%d/%d" % (b[0], b[1]), 13) + rpad("%.1f%%" % (b[1] / b[0] * 100 if b[0] else 0), 11))

    # ─────────── [5] 叫冷就冷：叫客胜 ───────────
    print("\n[5] 叫冷就冷：叫客胜（少数方向，自然频率 %.1f%%）" % (freq[2] * 100))
    print("  " + pad("条件", 26) + rpad("叫客", 7) + rpad("中客", 7) + rpad("命中率", 9) + "95%CI")
    print("  " + "-" * 72)
    for T in [0.35, 0.40, 0.45, 0.50, 0.55]:
        sub = [r for r in rows if r[1] and r[1].index(max(r[1])) == 2 and max(r[1]) >= T]
        hit = sum(1 for r in sub if r[0] == 2)
        m = len(sub)
        lo, hi = wilson(hit, m)
        print("  " + pad("prob叫客胜, max>=%.2f" % T, 26) + rpad(str(m), 7) + rpad(str(hit), 7)
              + rpad("%.1f%%" % (hit / m * 100 if m else 0), 9) + "[%.1f,%.1f]" % (lo * 100, hi * 100))
    sub = [r for r in rows if r[1] and r[2]
           and r[1].index(max(r[1])) == 2 and r[2].index(max(r[2])) == 2]
    hit = sum(1 for r in sub if r[0] == 2)
    m = len(sub)
    lo, hi = wilson(hit, m)
    print("  " + pad("双头一致叫客胜", 26) + rpad(str(m), 7) + rpad(str(hit), 7)
          + rpad("%.1f%%" % (hit / m * 100 if m else 0), 9) + "[%.1f,%.1f]" % (lo * 100, hi * 100))

    print()


if __name__ == "__main__":
    main()
