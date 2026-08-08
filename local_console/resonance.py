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


def compute_signal_votes(snapshot: dict[str, object]) -> dict[str, object]:
    """多策略方向投票（king-v2 多策略投票引擎精华的轻量版）。

    king-v2 用 9+ 信号（突破/回调/挤压/Supertrend/Vegas/MACD/Keltner/Ichimoku…）
    各自投票、多数一致才开仓。这里用快照里已具备的确定性字段实现 4 类信号：
        trend    趋势跟随：K 线方向与 4 根动量同号才投票（复用 _timeframe_vote）
        breakout 突破信号：breakout_up/breakout_down（区间前高/前低突破）
        pullback 回调信号：range_location_8 极性（低位做多/高位做空的前提）
        macd     动量信号：macd_histogram 正负（h1 优先，m15 兜底）
    每类信号按时间框架权重汇总为一票，再聚合 consensus∈[-1,1]。
    与 compute_resonance 的区别：共振按"时间框架"投票，本函数按"策略类型"投票。
    """
    structure = snapshot.get("timeframe_structure")
    if not isinstance(structure, dict) or not structure:
        return {"available": False, "reason": "快照缺少 timeframe_structure"}

    # 1) trend：复用各框架 _timeframe_vote，加权为净方向。
    weighted_sum = 0.0
    total_weight = 0
    for timeframe, weight in TIMEFRAME_WEIGHTS.items():
        frame = structure.get(timeframe)
        if isinstance(frame, dict):
            vote = _timeframe_vote(frame)
            weighted_sum += vote * weight
            total_weight += weight
    trend_vote = round(weighted_sum / total_weight, 3) if total_weight else 0.0

    # 2) breakout：h1 优先、m15 兜底（更高框架的区间突破更可靠）。
    breakout_vote = 0
    for timeframe in ("h1", "m15", "m5"):
        frame = structure.get(timeframe)
        if not isinstance(frame, dict):
            continue
        if frame.get("breakout_up") is True:
            breakout_vote = 1
            break
        if frame.get("breakout_down") is True:
            breakout_vote = -1
            break

    # 3) pullback：range_location_8 在区间低位偏多、高位偏空（回调入场前提）。
    pullback_vote = 0
    for timeframe in ("h1", "m15", "m5"):
        frame = structure.get(timeframe)
        if not isinstance(frame, dict) or not isinstance(frame.get("range_location_8"), (int, float)):
            continue
        location = float(frame["range_location_8"])
        if location <= 0.3:
            pullback_vote = 1
        elif location >= 0.7:
            pullback_vote = -1
        break

    # 4) macd：柱值正负即方向（h1 优先、m15/m5 兜底）。
    macd_vote = 0
    for timeframe in ("h1", "m15", "m5"):
        frame = structure.get(timeframe)
        if not isinstance(frame, dict) or not isinstance(frame.get("macd_histogram"), (int, float)):
            continue
        histogram = float(frame["macd_histogram"])
        if histogram > 0:
            macd_vote = 1
        elif histogram < 0:
            macd_vote = -1
        break

    signals: dict[str, int] = {
        "trend": 1 if trend_vote > 0 else -1 if trend_vote < 0 else 0,
        "breakout": breakout_vote,
        "pullback": pullback_vote,
        "macd": macd_vote,
    }
    voting = [vote for vote in signals.values() if vote != 0]
    if not voting:
        return {
            "available": True,
            "signals": signals,
            "consensus": 0.0,
            "label": "无信号一致",
            "note": "确定性事实：多策略信号投票（king-v2 精华），当前无有效信号。",
        }
    consensus = round(sum(voting) / len(voting), 3)
    if consensus >= 0.5:
        label = "多策略一致偏多"
    elif consensus <= -0.5:
        label = "多策略一致偏空"
    else:
        label = "多策略分歧"
    return {
        "available": True,
        "signals": signals,
        "consensus": consensus,
        "label": label,
        "voting_signals": len(voting),
        "note": "确定性事实：多策略信号投票（趋势/突破/回调/MACD），非模型推断。",
    }


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
