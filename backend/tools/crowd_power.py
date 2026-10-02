"""彩民集中度幂律 m_i = k * P_i^a 的 a 估计 —— 回答「a=1.369 稳不稳」.

模型:  m_i = P_i^a / S(a),  S(a) = Sum_j P_j^a = Prod_k Sum_o q_{k,o}^a  (闭式, 不用枚举 3^14)
  一等奖(14/14 全中) 期望注数  E1 = T * P_win^a / S(a)
  二等奖(13/14, 28 个组合) 期望  E2 = T * Sum2 P_j^a / S(a),  Sum2 P_j^a = P_win^a * Sum_j u_j^a
        (u_j = q_{k,alt} / q_{k,win}, 其余 13 场约掉)
  T = 总注数 = 销量/2;  销量 = 二等奖 totalPrizeamount / 0.192
  (用比例而非绝对量, 因为 P_win 只有 1e-7 量级, 直接算会下溢)

🔴 别逐期解 a:  方程 h(a) = log(N1/N2) + log(Sum u^a) 在 u_j > 1 时
  (实际结果是冷门的那几场) 到大 a 会重新上升 -> U 形 -> 双根或无根, 二分必错。
  **必须 pooled**: 两式各自在全部期次上聚合定一个 a, 两式之差 = 规格检验。

🔴 估计量: 报 **Poisson MLE**, 别报对数残差 LS。
  LS 只能吃 N>0 的期次, 而那 26 期「一等奖 0 注」的滚存期携带最强信息
  (冷门期 -> N1=0 -> 推高 a); 丢掉它们把 a 拽低 0.28
  (实测同一个方程: LS 1.215 vs Poisson 1.490)。n=0 的 Poisson 贡献 -E, 不会 log(0)。

📌 2026-09-29 结论(用户问「a=1.369 稳不稳」): 结构稳(两式差 CI 含 0、逐年 ±0.16),
  水平不稳(逐年 1.12~1.93)。**a 报区间 1.2~1.5**。
  配套 tools/crowd_roi.py 扫 a 出 ROI 上界 —— 那个上界对 a 是刀锋, 见记忆
  football-sfc-crowd-concentration。

用法: PYTHONPATH=. .venv/bin/python tools/crowd_power.py [--boot=2000]
"""
import json, math, random, sys
import numpy as np
from app.db.session import SessionLocal
from sqlalchemy import text

IDX = {"3": 0, "1": 1, "0": 2}
AG = np.arange(0.30, 4.0001, 0.002)      # a 的搜索网格


def q_prop(o):
    q = [1.0 / x for x in o]; s = sum(q)
    return [x / s for x in q]


def q_add(o):
    q = [1.0 / x for x in o]; s = sum(q)
    return [x - (s - 1.0) / 3 for x in q]


def q_pow(o):
    q = [1.0 / x for x in o]; lo, hi = 1e-6, 100.0
    for _ in range(300):
        k = math.sqrt(lo * hi)
        if sum(x ** k for x in q) - 1.0 > 0: lo = k
        else: hi = k
    return [x ** k for x in q]


def num(s):
    return float(str(s).replace(",", ""))


def load():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT m.issue, m.no, m.had_h, m.had_d, m.had_a, m.cz_result, m.match_date
        FROM sfc_issue_matches m
        WHERE m.issue IN (SELECT issue FROM sfc_issue_matches GROUP BY issue
              HAVING COUNT(*)=14
                 AND SUM(had_h IS NOT NULL AND had_d IS NOT NULL AND had_a IS NOT NULL)=14
                 AND SUM(cz_result IN ('3','1','0'))=14)
        ORDER BY m.issue, m.no""")).mappings().all()
    PRI = {r["issue"]: r["lottery_prize_json"] for r in db.execute(
        text("SELECT issue, lottery_prize_json FROM sfc_issues")).mappings()}
    db.close()
    per = {}
    for r in rows:
        per.setdefault(r["issue"], []).append(r)
    return per, PRI


def prep(issue, mr, devig, PRI):
    j = json.loads(PRI.get(issue) or "null")
    if not j:
        return None
    n1 = n2 = pay2 = None
    for p in j:
        if p.get("awardType") != 0:
            continue
        if p["prizeLevel"] == "一等奖": n1 = num(p["stakeCount"])
        if p["prizeLevel"] == "二等奖":
            n2 = num(p["stakeCount"]); pay2 = num(p["totalPrizeamount"])
    if not pay2:
        return None
    Q = []; idxs = []; win = 1.0; md = ""
    for r in sorted(mr, key=lambda x: x["no"]):
        o = [float(r["had_h"]), float(r["had_d"]), float(r["had_a"])]
        if min(o) <= 1.0:
            return None
        q = devig(o)
        if min(q) <= 0:
            return None
        Q.append(q); i = IDX[r["cz_result"]]; idxs.append(i); win *= q[i]
        md = str(r["match_date"] or "")
    us = [Q[k][alt] / Q[k][idxs[k]] for k in range(14) for alt in range(3) if alt != idxs[k]]

    # --- 一次性把整条 a 网格上的残差算好, 之后 bootstrap 只是按行求和 ---
    # 🔴 是 q**a 不是 exp(a*q)!  写成 exp(a*q) 会让 logS 偏大约 +20,
    #    实测表现 = 残差整片 +27 且 argmin 撞网格下界 (2026-09-29 踩过).
    #    自检: logS(a=1) 必须恰好为 0 (Sum_j P_j = 1).
    qarr = np.array(Q); uarr = np.array(us)
    logS = np.log((qarr[None, :, :] ** AG[:, None, None]).sum(axis=2)).sum(axis=1)
    lu = np.log((uarr[None, :] ** AG[:, None]).sum(axis=1))
    lT = math.log((pay2 / 0.192) / 2.0)
    lw = math.log(win)
    e1 = math.log(n1) - lT - AG * lw + logS if n1 and n1 > 0 else None
    e2 = math.log(n2) - lT - lu - AG * lw + logS if n2 and n2 > 0 else None

    # --- Poisson 对数似然矩阵 (每行一期的整条 a 网格) ---
    # 🔴 计数数据必须用 Poisson 似然, 不能用对数残差平方和。
    #    差别全在 n=0 的期次: LS 只能丢掉它们, 而那 26 期「一等奖 0 注」的滚存期
    #    恰好携带最强的信息(冷门期 -> N1=0 -> 推高 a)。丢掉会把 a 拽低 0.28
    #    (实测 LS 1.215 vs Poisson 1.490, 同一个方程)。n=0 时贡献 -E, 不会 log(0)。
    E1m = np.exp(lT + AG * lw - logS)
    E2m = np.exp(lT + AG * lw + lu - logS)
    p1 = (n1 * np.log(np.maximum(E1m, 1e-300)) - E1m) if n1 and n1 > 0 else -E1m
    p2 = (n2 * np.log(np.maximum(E2m, 1e-300)) - E2m) if n2 and n2 > 0 else -E2m
    return dict(issue=issue, n1=n1 or 0.0, n2=n2 or 0.0, md=md, e1=e1, e2=e2, p1=p1, p2=p2)


def est(E1, E2, which, idx=None):
    """对数残差最小二乘(旧法). 🔴 有偏: 只能用 n>0 的期次, 见 prep() 里的说明."""
    rows = idx if idx is not None else range(len(E1))
    M = [ (E1 if which == "n1" else E2)[i] for i in rows ]
    M = [m for m in M if m is not None]
    if not M:
        return None, 0
    S = (np.array(M) ** 2).sum(axis=0)
    k = int(np.argmin(S))
    return float(AG[k]), len(M)


def mle(P, which, idx=None):
    """Poisson 极大似然(正确). P = p1/p2 的 (n期, n网格) 对数似然矩阵."""
    rows = idx if idx is not None else range(len(P))
    M = [P[i] for i in rows]
    if not M:
        return None, 0
    return float(AG[int(np.argmax(np.array(M).sum(axis=0)))]), len(M)


def main():
    boot = 2000
    if "--boot" in sys.argv:
        boot = int(sys.argv[sys.argv.index("--boot") + 1])
    per, PRI = load()
    print(f"14 场齐全的期数: {len(per)}\n")
    random.seed(21)

    keep = None
    for name, dg in [("等比", q_prop), ("等量减法", q_add), ("power", q_pow)]:
        D = [x for x in (prep(i, mr, dg, PRI) for i, mr in sorted(per.items())) if x]
        L1 = [d["e1"] for d in D]; L2 = [d["e2"] for d in D]
        P1 = [d["p1"] for d in D]; P2 = [d["p2"] for d in D]
        m1, _ = mle(P1, "n1"); m2, _ = mle(P2, "n2")
        s1, n1 = est(L1, L2, "n1"); s2, n2 = est(L1, L2, "n2")
        extra = ""
        if name == "等比":
            keep = D
            z = sum(1 for d in D if d["n1"] <= 0)
            extra = f"   [一等奖0注(滚存) {z} 期]"
        print(f"  {name:8s} n={len(D):>4}{extra}")
        print(f"      Poisson MLE(正确)   a1={m1:.3f}  a2={m2:.3f}   差 {m1-m2:+.3f}")
        print(f"      对数残差 LS(有偏)   a1={s1:.3f}(用{n1}期)  a2={s2:.3f}(用{n2}期)   "
              f"差 {s1-s2:+.3f}")

    P1 = [d["p1"] for d in keep]; P2 = [d["p2"] for d in keep]
    print(f"\n=== 等比: bootstrap ({boot} 次, 重抽期次, Poisson MLE) ===")
    n = len(keep)
    B1 = []; B2 = []; BD = []
    for _ in range(boot):
        s = [random.randrange(n) for _ in range(n)]
        x1, _ = mle(P1, "n1", s); x2, _ = mle(P2, "n2", s)
        B1.append(x1); B2.append(x2); BD.append(x1 - x2)
    for lab, B in [("a1", B1), ("a2", B2), ("a1-a2", BD)]:
        B = sorted(B)
        lo, hi = B[int(len(B) * .025)], B[int(len(B) * .975)]
        if lab == "a1-a2":
            mark = "  含0 -> 两式自洽(幂律形式没被否定)" if lo < 0 < hi else "  不含0 -> 两式不一致"
        else:
            mark = f"  P(a>1)={sum(1 for x in B if x > 1) / len(B) * 100:.1f}%"
        print(f"  {lab:6s} {sum(B)/len(B):.3f}  CI95 [{lo:.3f},{hi:.3f}]{mark}")

    print("\n=== 逐年 (等比, Poisson MLE) ===")
    for y in sorted({d["md"][:4] for d in keep}):
        sub = [i for i, d in enumerate(keep) if d["md"][:4] == y]
        if len(sub) < 4:
            continue
        x1, _ = mle(P1, "n1", sub); x2, _ = mle(P2, "n2", sub)
        z = sum(1 for i in sub if keep[i]["n1"] <= 0)
        print(f"  {y}  n={len(sub):>3}  a1={x1:.3f}  a2={x2:.3f}  差 {x1-x2:+.3f}"
              f"   [一等奖0注 {z}/{len(sub)}]")


if __name__ == "__main__":
    main()
