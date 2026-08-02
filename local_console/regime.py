"""Deterministic market-regime classification (ADX / StdDev / RSI) from closed bars.

把"趋势跟踪浮盈加仓 EA"的三要素参数工程（ADX 双周期过滤、StdDev 波动确认、
RSI 超买超卖）实现为**确定性事实**喂给模型：M15 与 H1 的 ADX 同时 ≥25 判为
强趋势市、同时 <20 判为震荡市，中间为过渡市；再以 StdDev 确认波动放大、以
RSI 极端值标记禁追方向。全部由已收盘 K 线复算，非模型推断——与 resonance.py
同一套"确定性事实"哲学，但维度是市场状态而非方向。
"""

from __future__ import annotations

# 双周期趋势判定：M15 与 H1 同时满足才升级为趋势市（趋势 EA 精华）。
REGIME_TIMEFRAMES = ("m15", "h1")

TREND_ADX_THRESHOLD = 25.0  # 双周期 ADX 均 ≥ 25 → 强趋势市
RANGE_ADX_THRESHOLD = 20.0  # 双周期 ADX 均 < 20 → 震荡市（禁强方向）
STDDEV_CONFIRM_THRESHOLD = 1.2  # stddev_20 > 1.2 → 波动放大确认
RSI_OVERBOUGHT = 85.0  # ≥ 85 → 超买，禁追多（趋势 EA 平多阈值）
RSI_OVERSOLD = 15.0  # ≤ 15 → 超卖，禁追空


def _trend_vote(frame: object) -> int:
    """单个时间框架的趋势方向票：+1 多 / -1 空 / 0 方向不明。

    与 resonance._timeframe_vote 同规则：K 线方向（body_direction）与 4 根
    动量（change_4 符号）同号才投票，异号视为方向不明。
    """
    if not isinstance(frame, dict):
        return 0
    body = frame.get("body_direction")
    body_vote = 1 if body == "buy" else -1 if body == "sell" else 0
    change_4 = frame.get("change_4")
    if not isinstance(change_4, (int, float)):
        return body_vote
    momentum_vote = 1 if change_4 > 0 else -1 if change_4 < 0 else 0
    if body_vote == 0:
        return momentum_vote
    if momentum_vote == 0:
        return body_vote
    return body_vote if body_vote == momentum_vote else 0


def _classify_regime(adx_values: dict[str, float]) -> str:
    """按双周期 ADX 判市场状态：trending / ranging / transition / unknown。"""
    if len(adx_values) == 2:
        if all(value >= TREND_ADX_THRESHOLD for value in adx_values.values()):
            return "trending"
        if all(value < RANGE_ADX_THRESHOLD for value in adx_values.values()):
            return "ranging"
        return "transition"
    if adx_values:
        value = next(iter(adx_values.values()))
        if value >= TREND_ADX_THRESHOLD:
            return "trending"
        if value < RANGE_ADX_THRESHOLD:
            return "ranging"
        return "transition"
    return "unknown"


def compute_market_regime(snapshot: dict[str, object]) -> dict[str, object]:
    """从快照的 timeframe_structure 计算市场状态事实。

    返回结构（available=True 时）：
        regime         trending（强趋势）/ ranging（震荡）/ transition（过渡）/ unknown
        trend_direction  强趋势市时由 M15/H1 方向一致性给出的趋势方向（buy/sell），
                        transition/ranging 时为 None
        adx / rsi / stddev   各时间框架指标值明细（{m15: ..., h1: ...}，缺失的框架省略）
        volatility_confirmed  任一框架 stddev_20 超阈值 → 波动放大
        rsi_extreme     最极端的 RSI 标记（{"timeframe", "side", "value"}）或 None
    """
    structure = snapshot.get("timeframe_structure")
    if not isinstance(structure, dict) or not structure:
        return {"available": False, "reason": "快照缺少 timeframe_structure"}

    frames: dict[str, dict[str, float]] = {}
    for timeframe in REGIME_TIMEFRAMES:
        frame = structure.get(timeframe)
        if not isinstance(frame, dict):
            continue
        entry: dict[str, float] = {}
        for key, target in (("adx_14", "adx"), ("rsi_14", "rsi"), ("stddev_20", "stddev")):
            value = frame.get(key)
            if isinstance(value, (int, float)):
                entry[target] = float(value)
        if entry:
            frames[timeframe] = entry
    if not frames:
        return {"available": False, "reason": "无 ADX/RSI/StdDev 指标数据"}

    adx_values = {tf: entry["adx"] for tf, entry in frames.items() if "adx" in entry}
    regime = _classify_regime(adx_values)

    # 强趋势市才给出趋势方向；按高权重框架优先，方向票规则与共振一致。
    trend_direction: str | None = None
    if regime == "trending":
        for timeframe in REGIME_TIMEFRAMES:
            vote = _trend_vote(structure.get(timeframe))
            if vote:
                trend_direction = "buy" if vote > 0 else "sell"
                break

    # RSI 极端标记：取最极端的一个框架（超买取最高、超卖取最低），供禁追规则使用。
    rsi_extreme: dict[str, object] | None = None
    rsi_values = [(tf, entry["rsi"]) for tf, entry in frames.items() if "rsi" in entry]
    if rsi_values:
        overbought = [(tf, v) for tf, v in rsi_values if v >= RSI_OVERBOUGHT]
        oversold = [(tf, v) for tf, v in rsi_values if v <= RSI_OVERSOLD]
        if overbought or oversold:
            if overbought:
                timeframe, value = max(overbought, key=lambda pair: pair[1])
                rsi_extreme = {"timeframe": timeframe, "side": "overbought", "value": value}
            else:
                timeframe, value = min(oversold, key=lambda pair: pair[1])
                rsi_extreme = {"timeframe": timeframe, "side": "oversold", "value": value}

    volatility_confirmed = any(
        entry.get("stddev", 0.0) > STDDEV_CONFIRM_THRESHOLD
        for entry in frames.values()
        if "stddev" in entry
    )

    label = {
        "trending": "强趋势市",
        "ranging": "震荡市",
        "transition": "过渡市",
        "unknown": "状态不明",
    }[regime]

    return {
        "available": True,
        "regime": regime,
        "label": label,
        "trend_direction": trend_direction,
        "adx": adx_values,
        "rsi": {tf: entry["rsi"] for tf, entry in frames.items() if "rsi" in entry},
        "stddev": {tf: entry["stddev"] for tf, entry in frames.items() if "stddev" in entry},
        "volatility_confirmed": volatility_confirmed,
        "rsi_extreme": rsi_extreme,
        "note": "确定性事实：由已收盘K线 ADX/StdDev/RSI 复算的市场状态，非模型推断。",
    }
