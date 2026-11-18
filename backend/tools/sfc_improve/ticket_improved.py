"""
胜负彩 14 场复式出票改进版
目标：在原有 DP 最大化期望覆盖的基础上，加入平局感知和可切换策略，提升覆盖命中率。

使用方式：
  1. 准备每期 14 场的概率（市场去水 or 模型）
  2. 调用 ticket_sets_improved(...)
  3. 用历史彩果做回测对比

策略开关：
  - "market": 纯市场（与原逻辑接近）
  - "draw_sensitive": 平局敏感（推荐先试）
  - "model_blend": 市场 + 模型融合（需要同时传入 model_ps）
"""

import numpy as np
from typing import List, Tuple, Dict, Optional, Literal

Strategy = Literal["market", "draw_sensitive", "model_blend"]

# 胜负彩记法：索引 0=客胜(0), 1=平(1), 2=主胜(3)
SYM = ("0", "1", "3")


def _devig_simple(odds_h: float, odds_d: float, odds_a: float) -> np.ndarray:
    """简单去水（归一化）。实际可换成 Shin / power method。"""
    if min(odds_h, odds_d, odds_a) <= 1.0:
        return np.array([0.33, 0.34, 0.33])
    inv = np.array([1/odds_h, 1/odds_d, 1/odds_a])
    return inv / inv.sum()


def _ticket_cands_market(q: np.ndarray) -> List[Tuple[int, List[int], float]]:
    """原逻辑：按概率从高到低取 1/2/3 个。"""
    order = np.argsort(-q)
    out = []
    for k in (1, 2, 3):
        sel = sorted(order[:k].tolist())
        cov = float(q[sel].sum())
        out.append((k, sel, cov))
    return out


def _ticket_cands_draw_sensitive(
    q: np.ndarray,
    draw_threshold: float = 0.22,
    gap_threshold: float = 0.10,
    force_draw_when_close: bool = True,
) -> List[Tuple[int, List[int], float]]:
    """
    平局敏感候选生成。
    - 如果平局概率 >= draw_threshold，或最高与平局差距 < gap_threshold，
      则在双选时强制包含平局。
    - 单选仍然选最高概率（除非平局本身最高）。
    """
    order = np.argsort(-q)
    top1 = int(order[0])
    top2 = int(order[1])

    # 基础候选
    cands = []

    # 1. 单选：永远选概率最高的
    cands.append((1, [top1], float(q[top1])))

    # 2. 双选：优先考虑是否强制盖平
    draw_idx = 1  # 平局固定索引
    p_draw = q[draw_idx]
    p_top = q[top1]
    gap = p_top - p_draw

    if force_draw_when_close and (p_draw >= draw_threshold or gap < gap_threshold):
        # 强制双选包含平局 + 另一个最高（如果平局不是最高）
        if top1 == draw_idx:
            other = top2
        else:
            other = top1
        sel = sorted([draw_idx, other])
        cov = float(q[sel].sum())
        cands.append((2, sel, cov))
    else:
        # 正常按概率取前二
        sel = sorted([top1, top2])
        cov = float(q[sel].sum())
        cands.append((2, sel, cov))

    # 3. 三选：全包
    cands.append((3, [0, 1, 2], 1.0))

    return cands


def _blend_probs(market: np.ndarray, model: np.ndarray, alpha: float = 0.4) -> np.ndarray:
    """简单线性融合。alpha = 模型权重。"""
    if model is None or len(model) != 3:
        return market
    blended = (1 - alpha) * market + alpha * model
    return blended / blended.sum()


def ticket_sets_improved(
    probs: List[np.ndarray],
    budget_yuan: int = 64,
    strategy: Strategy = "draw_sensitive",
    model_probs: Optional[List[np.ndarray]] = None,
    draw_threshold: float = 0.22,
    gap_threshold: float = 0.10,
    blend_alpha: float = 0.4,
) -> Optional[Tuple[List, int, float]]:
    """
    改进版复式出票。

    参数:
        probs: 每场的市场去水概率 [p0, p1, p3]，长度 14
        budget_yuan: 预算（元），实际最大注数 = budget // 2
        strategy: "market" | "draw_sensitive" | "model_blend"
        model_probs: 模型概率（仅 model_blend 时使用）
        ...

    返回:
        (path, notes, expect_hits)
        path[i] = (k, selected_indices, coverage)
    """
    if len(probs) != 14:
        raise ValueError("必须正好 14 场")

    max_notes = budget_yuan // 2
    Q = []

    for i, q in enumerate(probs):
        q = np.asarray(q, dtype=float)
        if strategy == "model_blend" and model_probs is not None and i < len(model_probs):
            q = _blend_probs(q, np.asarray(model_probs[i]), blend_alpha)

        if strategy == "draw_sensitive":
            cands = _ticket_cands_draw_sensitive(q, draw_threshold, gap_threshold)
        else:
            cands = _ticket_cands_market(q)

        Q.append(cands)

    # DP: 注数 -> (累计期望命中, path)
    dp: Dict[int, Tuple[float, list]] = {1: (0.0, [])}

    for cands in Q:
        nxt = {}
        for notes, (val, path) in dp.items():
            for k, sel, cov in cands:
                n = notes * k
                if n > max_notes:
                    continue
                v = val + cov
                if n not in nxt or v > nxt[n][0]:
                    nxt[n] = (v, path + [(k, sel, cov)])
        dp = nxt
        if not dp:
            return None

    if not dp:
        return None

    notes, (expect_hits, path) = max(dp.items(), key=lambda kv: kv[1][0])
    return path, notes, float(expect_hits)


def face_to_string(path) -> str:
    """把 path 转成可读票面，例如 '3 1/0 3 ...' """
    parts = []
    for k, sel, _ in path:
        labels = [SYM[j] for j in sel]
        parts.append("/".join(sorted(labels)) if k > 1 else labels[0])
    return " ".join(parts)


# ==================== 示例 / 回测骨架 ====================

def example_usage():
    """演示如何调用。实际回测时替换成真实数据。"""
    # 模拟 14 场市场去水概率
    np.random.seed(42)
    market_probs = []
    for _ in range(14):
        raw = np.random.dirichlet([2, 1.5, 2])
        market_probs.append(raw)

    # 模拟模型概率（略有不同）
    model_probs = [np.random.dirichlet([2.2, 1.3, 2.1]) for _ in range(14)]

    print("=== 市场纯概率 ===")
    res = ticket_sets_improved(market_probs, 64, strategy="market")
    if res:
        path, notes, exp = res
        print(f"注数={notes}, 期望命中={exp:.2f}")
        print("票面:", face_to_string(path))

    print("\n=== 平局敏感 ===")
    res = ticket_sets_improved(market_probs, 64, strategy="draw_sensitive")
    if res:
        path, notes, exp = res
        print(f"注数={notes}, 期望命中={exp:.2f}")
        print("票面:", face_to_string(path))

    print("\n=== 模型融合 ===")
    res = ticket_sets_improved(
        market_probs, 64, strategy="model_blend", model_probs=model_probs, blend_alpha=0.35
    )
    if res:
        path, notes, exp = res
        print(f"注数={notes}, 期望命中={exp:.2f}")
        print("票面:", face_to_string(path))


if __name__ == "__main__":
    example_usage()
