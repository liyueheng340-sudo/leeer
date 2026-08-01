"""Deterministic multi-timeframe direction resonance computed from closed bars.

把 M5~H4 各时间框架的趋势一致性算成**确定性事实**喂给模型：每个时间框架
仅在"已收盘K线方向（body_direction）"与"4 根动量（change_4 符号）"同号时
投出一票，再按更高时间框架更重的权重汇总为 score∈[-1, 1]。目的是压缩模型
在方向判断上的自由发挥、提升结论稳定性——这是可复算的事实，不是模型推断。
"""

from __future__ import annotations

# 更高时间框架的趋势更可靠，权重更大；总和用于归一化。
TIMEFRAME_WEIGHTS = {"h4": 4, "h1": 3, "m15": 2, "m5": 1}

RESONANCE_BULL_THRESHOLD = 0.5
RESONANCE_BEAR_THRESHOLD = -0.5


def _timeframe_vote(frame: object) -> int:
    """单个时间框架的方向投票：+1 多 / -1 空 / 0 方向不明。

    仅当 K 线方向与 4 根动量同号时投票，异号视为该框架方向不明（0），
    避免用相互矛盾的信号污染共振判断。
    """
    if not isinstance(frame, dict):
        return 0
    body = frame.get("body_direction")
    body_vote = 1 if body == "buy" else -1 if body == "sell" else 0
    change_4 = frame.get("change_4")
    if not isinstance(change_4, (int, float)):
        return body_vote  # 缺动量时退化为单根 K 线方向
    momentum_vote = 1 if change_4 > 0 else -1 if change_4 < 0 else 0
    if body_vote == 0:
        return momentum_vote
    if momentum_vote == 0:
        return body_vote
    return body_vote if body_vote == momentum_vote else 0


def compute_resonance(snapshot: dict[str, object]) -> dict[str, object]:
    """从快照的 timeframe_structure 计算方向共振事实。

    返回结构（available=True 时）：
        score   加权净方向，[-1, 1]，正为多、负为空
        label   共振偏多 / 共振偏空 / 方向冲突 / 方向不明
        votes   各时间框架投票明细
        agreement  与净方向一致的投票占比
    """
    structure = snapshot.get("timeframe_structure")
    if not isinstance(structure, dict) or not structure:
        return {"available": False, "reason": "快照缺少 timeframe_structure"}

    votes: dict[str, int] = {}
    for timeframe in TIMEFRAME_WEIGHTS:
        if timeframe in structure:
            votes[timeframe] = _timeframe_vote(structure[timeframe])
    if not votes:
        return {"available": False, "reason": "无可用时间框架结构"}

    total_weight = 0
    weighted_sum = 0
    for timeframe, vote in votes.items():
        weight = TIMEFRAME_WEIGHTS[timeframe]
        total_weight += weight
        weighted_sum += vote * weight
    score = round(weighted_sum / total_weight, 3) if total_weight else 0.0

    voting = [vote for vote in votes.values() if vote != 0]
    if score >= RESONANCE_BULL_THRESHOLD:
        label = "共振偏多"
    elif score <= RESONANCE_BEAR_THRESHOLD:
        label = "共振偏空"
    elif voting:
        label = "方向冲突"
    else:
        label = "方向不明"

    if voting and score != 0:
        sign = 1 if score > 0 else -1
        aligned = sum(1 for vote in voting if (1 if vote > 0 else -1) == sign)
        agreement = round(aligned / len(voting), 3)
    else:
        agreement = 0.0

    return {
        "available": True,
        "score": score,
        "label": label,
        "votes": votes,
        "agreement": agreement,
        "voting_timeframes": len(voting),
        "note": "确定性事实：由已收盘K线方向与4根动量一致性加权计算，非模型推断。",
    }
