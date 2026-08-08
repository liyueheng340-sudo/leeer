"""Deterministic MACD divergence detection from closed-bar series.

把"MACD 背离指示器"（EA 文件夹指标）的核心逻辑实现为确定性事实：
- 对 M5/M15/H1 各算 MACD(12,26,9) 柱值序列；
- 找价格摆动高点/低点（左右 N 根验证），对比对应位置的 MACD 柱值；
- 价格创新高而 MACD 柱未创新高 → 顶背离（看跌警示）；
- 价格创新低而 MACD 柱未创新低 → 底背离（看涨警示）。

全部由已收盘 K 线复算，非模型推断——与 fractal_levels.py 同一套哲学。
"""

from __future__ import annotations

# MACD 参数（恒鑫/标准配置，与原 EA 指标一致）。
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
# 摆动点验证窗口：左右各 N 根（分形式局部极值）。
SWING_WINDOW = 2
# 至少需要两根可比对的摆动点才构成背离（一高一低或一低一高）。
MIN_DIVERGENCE_PAIRS = 2
# 价格创新高/新低的容差（价格单位）：越过该值才算"创新高/新低"。
PRICE_EXTREME_TOLERANCE = 0.5


def _macd_histogram_series(closes: list[float]) -> list[float]:
    """从收盘价序列计算 MACD 柱值序列（与 macd_series 同算法，输出全序列）。"""
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return []
    fast_multiplier = 2.0 / (MACD_FAST + 1)
    slow_multiplier = 2.0 / (MACD_SLOW + 1)
    signal_multiplier = 2.0 / (MACD_SIGNAL + 1)
    fast_ema = sum(closes[:MACD_FAST]) / MACD_FAST
    slow_ema = sum(closes[:MACD_SLOW]) / MACD_SLOW
    macd_values: list[float] = []
    for index in range(MACD_SLOW, len(closes)):
        fast_ema = (closes[index] - fast_ema) * fast_multiplier + fast_ema
        slow_ema = (closes[index] - slow_ema) * slow_multiplier + slow_ema
        macd_values.append(fast_ema - slow_ema)
    if len(macd_values) < MACD_SIGNAL:
        return []
    signal_ema = sum(macd_values[:MACD_SIGNAL]) / MACD_SIGNAL
    histogram: list[float] = []
    for index, value in enumerate(macd_values):
        if index >= MACD_SIGNAL:
            signal_ema = (value - signal_ema) * signal_multiplier + signal_ema
        histogram.append(value - signal_ema)
    return histogram


def _swing_highs(closes: list[float], window: int) -> list[int]:
    """局部高点索引：左右 window 根均不高于它。"""
    indices: list[int] = []
    for index in range(window, len(closes) - window):
        value = closes[index]
        if all(closes[index - offset] <= value for offset in range(1, window + 1)) and all(
            closes[index + offset] <= value for offset in range(1, window + 1)
        ):
            indices.append(index)
    return indices


def _swing_lows(closes: list[float], window: int) -> list[int]:
    """局部低点索引：左右 window 根均不低于它。"""
    indices: list[int] = []
    for index in range(window, len(closes) - window):
        value = closes[index]
        if all(closes[index - offset] >= value for offset in range(1, window + 1)) and all(
            closes[index + offset] >= value for offset in range(1, window + 1)
        ):
            indices.append(index)
    return indices


def _detect_side(
    closes: list[float], hist: list[float], swing_indices: list[int], *, bearish: bool
) -> dict[str, object] | None:
    """检测顶背离（bearish=True）或底背离（bearish=False）。

    顶背离：价格后高点 > 前高点（且相差超过容差），MACD 柱后高点 < 前高点。
    底背离：价格后低点 < 前低点，MACD 柱后低点 > 前低点。
    """
    if len(swing_indices) < MIN_DIVERGENCE_PAIRS:
        return None
    # 每根摆动点对应其 MACD 柱值（对齐收盘索引；柱序列从 MACD_SLOW 开始）。
    aligned = [(idx, hist[idx - MACD_SLOW]) for idx in swing_indices if idx - MACD_SLOW >= 0]
    if len(aligned) < MIN_DIVERGENCE_PAIRS:
        return None
    for i in range(1, len(aligned)):
        prev_idx, prev_hist = aligned[i - 1]
        curr_idx, curr_hist = aligned[i]
        prev_price = closes[prev_idx]
        curr_price = closes[curr_idx]
        if bearish:
            price_new_high = curr_price > prev_price + PRICE_EXTREME_TOLERANCE
            hist_lower = curr_hist < prev_hist
            if price_new_high and hist_lower:
                return {
                    "side": "bearish",
                    "label": "顶背离",
                    "price_highs": [round(prev_price, 2), round(curr_price, 2)],
                    "histogram_highs": [round(prev_hist, 6), round(curr_hist, 6)],
                    "note": "价格创出新高而 MACD 柱未创新高，上涨动能减弱（看跌警示，非方向结论）",
                }
        else:
            price_new_low = curr_price < prev_price - PRICE_EXTREME_TOLERANCE
            hist_higher = curr_hist > prev_hist
            if price_new_low and hist_higher:
                return {
                    "side": "bullish",
                    "label": "底背离",
                    "price_lows": [round(prev_price, 2), round(curr_price, 2)],
                    "histogram_lows": [round(prev_hist, 6), round(curr_hist, 6)],
                    "note": "价格创出新低而 MACD 柱未创新低，下跌动能减弱（看涨警示，非方向结论）",
                }
    return None


def compute_macd_divergence(snapshot: dict[str, object]) -> dict[str, object]:
    """从快照的 bar_series 计算各时间框架的 MACD 背离事实。

    返回结构（available=True 时）：
        divergences   {tf: {"side", "label", "price_*", "histogram_*", "note"} 或 None}
        any_divergence  任一框架出现背离
        note         确定性事实说明
    """
    series = snapshot.get("bar_series")
    if not isinstance(series, dict) or not series:
        return {"available": False, "reason": "快照缺少 bar_series"}

    divergences: dict[str, object] = {}
    any_divergence = False
    for timeframe in ("m5", "m15", "h1"):
        bars = series.get(timeframe)
        if not isinstance(bars, list) or len(bars) < MACD_SLOW + MACD_SIGNAL + SWING_WINDOW * 2 + 2:
            divergences[timeframe] = None
            continue
        closes = [float(bar["close"]) for bar in bars]
        hist = _macd_histogram_series(closes)
        if len(hist) < 12:
            divergences[timeframe] = None
            continue
        result = _detect_side(closes, hist, _swing_highs(closes, SWING_WINDOW), bearish=True)
        if result is None:
            result = _detect_side(closes, hist, _swing_lows(closes, SWING_WINDOW), bearish=False)
        if result is not None:
            any_divergence = True
        divergences[timeframe] = result

    return {
        "available": True,
        "divergences": divergences,
        "any_divergence": any_divergence,
        "note": "确定性事实：由已收盘K线 MACD 柱与摆动点复算的背离警示，非模型推断。",
    }
