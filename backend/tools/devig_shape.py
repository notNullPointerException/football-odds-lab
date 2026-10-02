"""看一家公司的抽水【形状】—— 这是清 9-28/9-29 devig 冲突的那个决定性测试.

为什么需要它: "哪个 devig 方法最好" 不是方法的属性, 是 (方法, 公司) 配对的属性.
等比归一化 = "公司对真实概率乘一个固定系数" 的精确逆.
所以判据是: **这家的水位随比赛悬殊程度怎么变?**

    比例抽水   q = p * (1+m)      -> 水位【处处相等】
    压冷门项   在冷门那端加固定量  -> 比赛越悬殊水位越低

体彩  = 定额比例 (6 档极差 0.04pp) -> 用等比
庄家  = 随场形变 (6 档极差 1.24pp) -> 等比不是正确的逆

用法:
    PYTHONPATH=. .venv/bin/python tools/devig_shape.py           # 体彩样本
    PYTHONPATH=. .venv/bin/python tools/devig_shape.py api       # 庄家样本(api-football 缓存)
"""
import json
import sys

sys.path.insert(0, ".")

from tools.devig_audit import q_of


def load_sfc():
    from app.db.session import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT had_h, had_d, had_a FROM sfc_issue_matches
        WHERE had_h IS NOT NULL AND had_d IS NOT NULL AND had_a IS NOT NULL
    """)).mappings().all()
    db.close()
    out = []
    for r in rows:
        o = [float(r["had_h"]), float(r["had_d"]), float(r["had_a"])]
        if min(o) > 1.0:
            out.append(o)
    return out


def load_api():
    """每场取水位最低(最 sharp)的那家, 避免多家/场造成的伪重复."""
    cache = json.load(open("/tmp/odds_by_date.json"))
    from app.db.session import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    FX = {r["id"]: dict(r) for r in db.execute(
        text("SELECT id,goals_home,goals_away,status_short FROM fixtures")).mappings()}
    db.close()
    out = []
    for d in cache.values():
        for k, v in d.items():
            g = FX.get(int(k))
            if not g or g["status_short"] != "FT" or g["goals_home"] is None:
                continue
            cands = [o for o in v["bk"].values() if 1.01 <= min(o) and max(o) <= 200]
            if cands:
                out.append(min(cands, key=lambda o: sum(q_of(o))))
    return out


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "sfc"
    O = load_api() if src == "api" else load_sfc()
    title = "庄家样本 (api-football, 每场取最 sharp 家)" if src == "api" else "体彩样本 (胜负彩 had)"

    D = sorted((max(q_of(o)), sum(q_of(o)) - 1.0) for o in O)
    n = len(D)
    ws = sorted(w for _, w in D)
    print(f"\n### {title}   n={n}   全样本水位中位 {ws[n // 2] * 100:.2f}%\n")
    print("  按【最大原始 1/o】分 6 档 -> 水位中位")
    print("  (比例抽水 = 平的; 水压冷门项 = 比赛越悬殊水位越低)\n")
    allw = []
    for b in range(6):
        ch = D[b * n // 6:(b + 1) * n // 6]
        mq = sum(x for x, _ in ch) / len(ch)
        mw = sorted(w for _, w in ch)[len(ch) // 2]
        allw.append(mw)
        print(f"    最大q {mq * 100:>5.1f}%   水位 {mw * 100:>5.2f}%  {'#' * int(mw * 300)}")
    print(f"\n    极差 {(max(allw) - min(allw)) * 100:.2f}pp   "
          f"{'<- 平 => 比例抽水 => 用等比' if max(allw) - min(allw) < 0.005 else '<- 随场变 => 等比不是正确的逆'}")


if __name__ == "__main__":
    main()
