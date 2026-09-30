"""胜负彩「最优买法 ROI 上界」对幂律参数 a 的敏感性 —— 回答「a 不确定要不要紧」.

彩池制定价: 组合 i 的彩民注数占比 m_i = P_i^a / S(a),  S(a)=Prod_k Sum_o q_{k,o}^a (闭式)
买 n 注全押组合 i:
    每注派奖 = min(0.448 * 销量 / (m_i*T + n), 500万)     销量 = 2T, T = 总注数
    ROI(P) = P * min(0.448*T / (m_i*T + n), 2.5e6) - 1     <- 每 2 元票
KEY: **P_i 与 a 无关** —— 每期枚举一次 3^14 个组合概率, 之后换 a / 换 n 只是重算一遍函数,
     不用重新枚举. 这就是这个脚本能跑完的原因.

🔴 别用 EV_i = P_i/m_i (会在 m_i->0 时发散, 我栽过三次: +49.9% / +98098% / +140% 全是伪影).
   真 payout 是 池子/(彩民票+我的票), 再加 500 万封顶 —— 两个上界都会剪.

用法: PYTHONPATH=. .venv/bin/python tools/crowd_roi.py
"""
import json
import math
import sys

import numpy as np

sys.path.insert(0, ".")

IDX = {"3": 0, "1": 1, "0": 2}
CAP = 5_000_000.0          # 单注封顶
NBIN = 50_000              # P 的分箱数(只要 max, 不用逐票)


def q_prop(o):
    q = [1.0 / x for x in o]
    s = sum(q)
    return [x / s for x in q]


def num(s):
    return float(str(s).replace(",", ""))


def load():
    from app.db.session import SessionLocal
    from sqlalchemy import text
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
    out = []
    for issue, mr in sorted(per.items()):
        j = json.loads(PRI.get(issue) or "null")
        if not j:
            continue
        n1 = n2 = pay2 = None
        for p in j:
            if p.get("awardType") != 0:
                continue
            if p["prizeLevel"] == "一等奖":
                n1 = num(p["stakeCount"])
            if p["prizeLevel"] == "二等奖":
                n2 = num(p["stakeCount"]); pay2 = num(p["totalPrizeamount"])
        if not pay2:
            continue
        Q = []
        win = 1.0
        bad = False
        for r in sorted(mr, key=lambda x: x["no"]):
            o = [float(r["had_h"]), float(r["had_d"]), float(r["had_a"])]
            if min(o) <= 1.0:
                bad = True; break
            q = q_prop(o)
            if min(q) <= 0:
                bad = True; break
            Q.append(q); win *= q[IDX[r["cz_result"]]]
        if bad:
            continue
        out.append(dict(issue=issue, q=Q, T=(pay2 / 0.192) / 2.0,
                        md=str(mr[0]["match_date"] or ""), win=win))
    return out


def all_probs(Q):
    """枚举 3^14 个组合概率. 反复做外积: 3^k -> 3^(k+1)."""
    P = np.array(Q[0])
    for k in range(1, 14):
        P = np.concatenate([P * Q[k][0], P * Q[k][1], P * Q[k][2]])
    return P


def Sa(Q, a):
    """S(a) = Prod_k Sum_o q^a —— 闭式, 不用枚举."""
    return float(np.prod([sum(x ** a for x in q) for q in Q]))


def main():
    REC = load()
    print(f"期数 {len(REC)}\n")
    AVALS = [1.12, 1.26, 1.37, 1.49, 1.60, 1.93]
    NS = [1, 10, 100, 1_000, 10_000, 100_000]

    print("=== 每期最大 ROI(在 n 上取最优), 按 a 分列 ===")
    print("    ROI = P*min(0.448*T/(m_i*T+n), 2.5e6) - 1 ;  已含池子上界 + 500万封顶\n")
    head = "  " + "a=" + "  ".join(f"{a:>7.2f}" for a in AVALS)
    print(f"{'年份':<7}{head}")

    res = {a: [] for a in AVALS}
    for r in REC:
        P = all_probs(r["q"])
        P = P[P > 0]
        lo, hi = math.log(P.min()), math.log(P.max())
        cnt, edges = np.histogram(np.log(P), bins=NBIN)
        ok = cnt > 0
        grid = np.exp((edges[:-1][ok] + edges[1:][ok]) / 2.0)   # 只留有组合落在里面的箱
        row = {}
        for a in AVALS:
            S = Sa(r["q"], a)
            best = -1e9
            for n in NS:
                mT = (grid ** a / S) * r["T"]                    # 彩民注数
                pay = np.minimum(0.448 * r["T"] / (mT + n), CAP / 2.0)
                roi = grid * pay - 1.0
                best = max(best, float(roi.max()))
            res[a].append(best); row[a] = best
        if len(res[AVALS[0]]) % 20 == 0:
            print("  " + r["md"][:10] + "  " + "  ".join(f"{row[a]*100:>+7.1f}%" for a in AVALS))

    print(f"\n=== 跨期汇总 (每期自己挑最优 n) ===")
    print(f"  {'a':>6} {'中位ROI':>10} {'最好的一期':>12} {'P(ROI>0)':>10} {'P(ROI>-36%)':>13}")
    for a in AVALS:
        v = sorted(res[a]); med = v[len(v) // 2]
        print(f"  {a:>6.2f} {med*100:>9.1f}% {v[-1]*100:>11.1f}% "
              f"{sum(1 for x in v if x > 0)/len(v)*100:>9.1f}% "
              f"{sum(1 for x in v if x > -0.36)/len(v)*100:>12.1f}%")


if __name__ == "__main__":
    main()
