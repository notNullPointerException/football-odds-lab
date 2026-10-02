"""清 devig 冲突的第二半: 把【庄家样本】和【体彩样本】放到同一把尺子上.

两把尺子:
  尺子1 = 按【与方法无关的原始 1/o】分桶 -> 排除分桶偏倚
  尺子2 = 配对 Brier, 按【比赛】配对 + 按比赛聚类 bootstrap
庄家样本有 7.3 家/场 -> 必须先按比赛聚合, 否则是伪重复 (pseudo-replication)
"""
import json, math, random, collections
import sys
sys.path.insert(0, ".")

from tools.devig_audit import devig_prop, devig_add, devig_pow, devig_shin, q_of, brier

METHODS = [("等比", devig_prop), ("等量减法", devig_add), ("power", devig_pow), ("Shin", devig_shin)]
random.seed(11)


def load_bookmakers():
    """-> 每场一行, odds = 取水位【最低】(最 sharp)的那家"""
    cache = json.load(open("/tmp/odds_by_date.json"))
    from app.db.session import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    FX = {r["id"]: dict(r) for r in db.execute(
        text("SELECT id,goals_home,goals_away,status_short FROM fixtures")).mappings()}
    db.close()
    flat = {int(k): v for d in cache.values() for k, v in d.items() if v["bk"]}
    out = []
    for f, v in flat.items():
        g = FX.get(f)
        if not g or g["status_short"] != "FT" or g["goals_home"] is None:
            continue
        cands = []
        for bk, o in v["bk"].items():
            if min(o) < 1.01 or max(o) > 200:
                continue
            cands.append((sum(q_of(o)) - 1.0, o))
        if not cands:
            continue
        w, o = min(cands)                      # sharpest bookmaker
        idx = 0 if g["goals_home"] > g["goals_away"] else (1 if g["goals_home"] == g["goals_away"] else 2)
        out.append(dict(key=f, o=o, idx=idx, water=w))
    return out


def ruler1(D, title, nb=8):
    print(f"\n--- 尺子1: 按与方法无关的原始 1/o 分 {nb} 个等量桶 — {title} ---")
    cq = [(d, k) for d in D for k in range(3)]
    cq.sort(key=lambda t: q_of(t[0]["o"])[t[1]])
    N = len(cq)
    print(f"   {'桶':>3} {'n':>7} {'原始q':>7} {'实际':>7}" + "".join(f" {n:>16}" for n, _ in METHODS))
    for b in range(nb):
        ch = cq[b * N // nb:(b + 1) * N // nb]
        raw = sum(q_of(d["o"])[k] for d, k in ch) / len(ch)
        act = sum(1 for d, k in ch if k == d["idx"]) / len(ch)
        line = f"   {b+1:>3} {len(ch):>7} {raw*100:>6.1f}% {act*100:>6.1f}%"
        for n, f in METHODS:
            cp = sum(f(d["o"])[k] for d, k in ch) / len(ch)
            line += f"  {cp*100:>6.1f}({(cp-act)*100:>+5.1f})"
        print(line)


def ruler2(D, title):
    print(f"\n--- 尺子2: 配对 Brier (按比赛配对, 聚类 bootstrap) — {title} ---")
    B = {n: [brier(f(d["o"]), d["idx"]) for d in D] for n, f in METHODS}
    for n, _ in METHODS:
        print(f"   {n:10s} Brier {sum(B[n])/len(B[n]):.5f}")
    print("   ── 相对等比 (负=更好) ──")
    for n, _ in METHODS[1:]:
        dif = [a - b for a, b in zip(B[n], B["等比"])]
        mu = sum(dif) / len(dif)
        bs = []
        for _ in range(3000):
            s = [dif[random.randrange(len(dif))] for _ in range(len(dif))]
            bs.append(sum(s) / len(s))
        bs.sort()
        sig = "*" if (bs[75] > 0 or bs[-75] < 0) else " "
        print(f"   {n:10s} {mu:+.5f}  [{bs[75]:+.5f},{bs[-75]:+.5f}]{sig}")


if __name__ == "__main__":
    api = load_bookmakers()
    print(f"庄家样本(按比赛聚合, 取最sharp家): {len(api)} 场   水位中位 "
          f"{sorted(d['water'] for d in api)[len(api)//2]*100:.2f}%")
    ruler1(api, "庄家样本")
    ruler2(api, "庄家样本")
