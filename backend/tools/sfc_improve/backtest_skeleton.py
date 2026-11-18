"""
回测骨架：把历史数据喂进来，对比不同策略的实际命中场数。

使用前需要你准备数据，格式示例：
issues = [
    {
        "issue": "26113",
        "market_probs": [ [0.4, 0.3, 0.3], ... ],  # 14 场
        "model_probs":  [ [0.45, 0.25, 0.3], ... ], # 可选
        "results": ["3", "1", "0", ...],            # 实际彩果，"3"/"1"/"0"
    },
    ...
]
"""

import numpy as np
from typing import List, Dict
from ticket_improved import ticket_sets_improved, face_to_string, SYM


def evaluate_issue(
    market_probs: List[np.ndarray],
    results: List[str],
    strategy: str = "draw_sensitive",
    budget: int = 64,
    model_probs=None,
    **kwargs,
) -> Dict:
    res = ticket_sets_improved(
        market_probs,
        budget_yuan=budget,
        strategy=strategy,
        model_probs=model_probs,
        **kwargs,
    )
    if res is None:
        return {"error": "无法生成票面"}

    path, notes, expect = res
    actual_hits = 0
    details = []

    for i, (k, sel, cov) in enumerate(path):
        actual = results[i]
        # 把实际彩果转成索引
        actual_idx = {"0": 0, "1": 1, "3": 2}.get(actual, -1)
        hit = actual_idx in sel
        if hit:
            actual_hits += 1
        details.append({
            "match": i + 1,
            "selected": [SYM[j] for j in sel],
            "actual": actual,
            "hit": hit,
            "coverage": round(cov, 3),
        })

    return {
        "notes": notes,
        "cost": notes * 2,
        "expect_hits": round(expect, 2),
        "actual_hits": actual_hits,
        "face": face_to_string(path),
        "details": details,
    }


def backtest_summary(issues: List[Dict], strategies=("market", "draw_sensitive"), budget=64):
    """对多期做对比汇总。"""
    summary = {s: [] for s in strategies}

    for issue in issues:
        m_probs = [np.array(p) for p in issue["market_probs"]]
        results = issue["results"]
        model = issue.get("model_probs")
        if model:
            model = [np.array(p) for p in model]

        for strat in strategies:
            r = evaluate_issue(
                m_probs, results, strategy=strat, budget=budget, model_probs=model
            )
            if "actual_hits" in r:
                summary[strat].append(r["actual_hits"])

    print(f"预算 {budget} 元，共 {len(issues)} 期")
    for strat, hits in summary.items():
        if hits:
            avg = np.mean(hits)
            print(f"  {strat:16s}  平均命中 {avg:.2f} 场  (期数 {len(hits)})")
    return summary


if __name__ == "__main__":
    print("回测骨架已就绪。请准备真实 issues 数据后调用 backtest_summary(...)")
