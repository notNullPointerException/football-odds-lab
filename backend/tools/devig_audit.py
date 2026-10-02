"""去水方法审计 — 一个脚本同时跑两套判据, 用来清 9-28(乘法最差) vs 9-29(等比最好) 的冲突.

  A. 配对 Brier (proper score, 最小化者=最接近真值)  -> 直接比大小
  B. 按【各方法自己的声称概率】分桶的校准表          -> 记忆里那张 65-80% 表的复现
  C. 按【与方法无关的原始 1/o】分桶的校准表          -> 排除分桶偏倚

用法: python tools/devig_audit.py [--source=sfc|api]
"""
import sys, math, json, random, collections

def q_of(o):
    return [1.0 / x for x in o]

def devig_prop(o):
    q = q_of(o); s = sum(q)
    return [x / s for x in q]

def devig_add(o):
    q = q_of(o); s = sum(q); n = len(q)
    return [x - (s - 1.0) / n for x in q]

def devig_pow(o):
    """power 法: 解 Sum(q^k)=1 定 k, 返回 q^k (按构造和恰为 1).
    🔴 别写错成返回未归一化的 q^k —— 那会让 Brier 变成 1.0 而看不出来."""
    q = q_of(o)
    lo, hi = 1e-6, 100.0
    for _ in range(300):
        k = math.sqrt(lo * hi)
        if sum(x ** k for x in q) - 1.0 > 0: lo = k   # 水还没去完 -> k 要更大
        else: hi = k
    return [x ** k for x in q]

def devig_shin(o):
    q = q_of(o); B = sum(q)
    lo, hi = 1e-12, 0.999999
    for _ in range(300):
        z = (lo + hi) / 2
        p = [(math.sqrt(z * z + 4 * (1 - z) * x * x / B) - z) / (2 * (1 - z)) for x in q]
        if sum(p) > 1: lo = z
        else: hi = z
    return [(math.sqrt(z * z + 4 * (1 - z) * x * x / B) - z) / (2 * (1 - z)) for x in q]

METHODS = [("等比", devig_prop), ("等量减法", devig_add), ("power", devig_pow), ("Shin", devig_shin)]

def brier(p, idx):
    return sum((p[k] - (1.0 if k == idx else 0.0)) ** 2 for k in range(3))

def load_sfc():
    from app.db.session import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT issue, no, had_h, had_d, had_a, cz_result, league, match_date
        FROM sfc_issue_matches
        WHERE had_h IS NOT NULL AND had_d IS NOT NULL AND had_a IS NOT NULL
          AND cz_result IN ('3','1','0')
    """)).mappings().all()
    db.close()
    D = []
    for r in rows:
        o = [float(r["had_h"]), float(r["had_d"]), float(r["had_a"])]
        if min(o) <= 1.0: continue
        idx = {"3": 0, "1": 1, "0": 2}[r["cz_result"]]
        D.append(dict(key=f'{r["issue"]}-{r["no"]}', o=o, idx=idx,
                      league=r["league"] or "", water=sum(q_of(o)) - 1.0))
    return D

def main():
    src = sys.argv[1].split("=")[1] if len(sys.argv) > 1 and "=" in sys.argv[1] else "sfc"
    D = load_sfc()
    print(f"样本 {len(D)} 场   水位 中位 {sorted(d['water'] for d in D)[len(D)//2]*100:.2f}%\n")

    B = {n: [] for n, _ in METHODS}
    for d in D:
        for n, f in METHODS:
            B[n].append(brier(f(d["o"]), d["idx"]))

    print("=== A. 配对 Brier (越小越好) ===")
    for n, _ in METHODS:
        m = sum(B[n]) / len(B[n])
        print(f"   {n:10s} Brier {m:.5f}")
    base = B["等比"]
    print("   ── 相对等比 (负=比等比好) ──")
    for n, _ in METHODS[1:]:
        dif = [a - b for a, b in zip(B[n], base)]
        mu = sum(dif) / len(dif)
        random.seed(7); bs = []
        for _ in range(2000):
            s = [dif[random.randrange(len(dif))] for _ in range(len(dif))]
            bs.append(sum(s) / len(s))
        bs.sort()
        sig = "*" if (bs[50] > 0 or bs[-50] < 0) else " "
        print(f"   {n:10s} {mu:+.5f}  [{bs[50]:+.5f},{bs[-50]:+.5f}]{sig}")

    print("\n=== B. 按【各方法自己的声称概率】分桶 (复现记忆里那张表) ===")
    print("   只保留水位 12-14% 的子样本, 对齐记忆里的口径")
    sub = [d for d in D if 0.12 <= d["water"] <= 0.14]
    print(f"   子样本 {len(sub)} 场\n")
    hdr = f"   {'档位':>10} {'n':>7} {'实际':>7}"
    for n, _ in METHODS: hdr += f" {n:>8}"
    print(hdr)
    # 桶由【等比】的声称概率定义; 各列是【该方法在同一个桶里的声称概率均值】
    # (这才是记忆里那张表的读法: 实际 83.1% vs 乘法 70.5%)
    PRE = {n: [f(d["o"]) for d in sub] for n, f in METHODS}
    PPROP = PRE["等比"]
    for lo, hi in [(0.65, 0.80), (0.80, 1.01), (0.45, 0.55), (0.35, 0.45), (0.15, 0.35), (0.05, 0.15)]:
        sel = [(i, k) for i, ps in enumerate(PPROP) for k in range(3) if lo <= ps[k] < hi]
        if not sel:
            print(f"   {f'{lo:.2f}-{hi:.2f}':>10} {0:>7}"); continue
        act = sum(1 for i, k in sel if k == sub[i]["idx"]) / len(sel)
        line = f"   {f'{lo:.2f}-{hi:.2f}':>10} {len(sel):>7} {act*100:>6.1f}%"
        for n, _ in METHODS:
            mp = sum(PRE[n][i][k] for i, k in sel) / len(sel)
            line += f" {mp*100:>7.1f}%"
        print(line)

    print("\n=== C. 按【与方法无关的原始 1/o】分桶 (排除分桶偏倚) ===")
    print("   按 q=1/o 排序切成 8 个等量桶, 桶内比较 各方法声称 vs 实际\n")
    cellq = [(d, k) for d in D for k in range(3)]
    cellq.sort(key=lambda t: q_of(t[0]["o"])[t[1]])
    N = len(cellq)
    print(f"   {'桶':>4} {'n':>8} {'原始q':>7} {'实际':>7}" + "".join(f" {n:>8}" for n, _ in METHODS))
    for b in range(8):
        chunk = cellq[b * N // 8:(b + 1) * N // 8]
        raw = sum(q_of(d["o"])[k] for d, k in chunk) / len(chunk)
        act = sum(1 for d, k in chunk if k == d["idx"]) / len(chunk)
        line = f"   {b+1:>4} {len(chunk):>8} {raw*100:>6.1f}% {act*100:>6.1f}%"
        for n, f in METHODS:
            cp = sum(f(d["o"])[k] for d, k in chunk) / len(chunk)
            line += f" {cp*100:>6.1f}%({(cp-act)*100:>+5.1f})"
        print(line)

    print("\n=== D. 按水位分层的 Brier 差 (负=比等比好) ===")
    for lo, hi in [(0, 0.06), (0.06, 0.10), (0.10, 0.13), (0.13, 0.20), (0.20, 9)]:
        sub2 = [i for i, d in enumerate(D) if lo <= d["water"] < hi]
        if len(sub2) < 30: continue
        line = f"   水位 {lo*100:.0f}-{hi*100:.0f}%  n={len(sub2):>6}"
        for n, _ in METHODS[1:]:
            m = sum(B[n][i] - B["等比"][i] for i in sub2) / len(sub2)
            line += f"   {n} {m:+.5f}"
        print(line)

if __name__ == "__main__":
    main()
