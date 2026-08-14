"""Shared MT5 read-only helpers.

This module deliberately contains no trade-write constants or calls so observer
code never has to import the legacy order-capable autotrader.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def atr(bars: list[dict[str, float]], period: int) -> float:
    if len(bars) < period + 1:
        raise ValueError("not_enough_bars_for_atr")
    true_ranges: list[float] = []
    for index in range(len(bars) - period, len(bars)):
        previous_close = bars[index - 1]["close"]
        high = bars[index]["high"]
        low = bars[index]["low"]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(true_ranges) / period


def _wilders_smooth(values: list[float], period: int) -> list[float]:
    """Wilder 平滑：首值为前 period 项之均值，其后按 (prev*(p-1)+cur)/p 递推。

    返回长度 = len(values) - period + 1；values 不足 period 项时返回空列表。
    修复（2026-08-07）：首值原为 sum 缺 /period，导致 ADX 被放大 period 倍、
    震荡市被误判为强趋势市（干净上涨序列曾返回 221，物理不可能）。
    """
    if len(values) < period:
        return []
    smoothed = [sum(values[:period]) / period]
    for value in values[period:]:
        smoothed.append((smoothed[-1] * (period - 1) + value) / period)
    return smoothed


def adx(bars: list[dict[str, float]], period: int = 14) -> float:
    """Wilder 平均趋向指数（ADX），取最后一根已收盘 K 线的值。

    ADX 衡量趋势强度（非方向）：>25 视为趋势市，<20 视为震荡市。
    需要至少 2*period+2 根 K 线才有稳定输出；不足时抛 ValueError。
    """
    if len(bars) < period * 2 + 2:
        raise ValueError("not_enough_bars_for_adx")
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    true_ranges: list[float] = []
    for index in range(1, len(bars)):
        high, low = bars[index]["high"], bars[index]["low"]
        prev_high, prev_low = bars[index - 1]["high"], bars[index - 1]["low"]
        prev_close = bars[index - 1]["close"]
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    tr_smooth = _wilders_smooth(true_ranges, period)
    plus_smooth = _wilders_smooth(plus_dm, period)
    minus_smooth = _wilders_smooth(minus_dm, period)
    dx_values: list[float] = []
    for tr, plus, minus in zip(tr_smooth, plus_smooth, minus_smooth, strict=True):
        if tr <= 0 or (plus + minus) <= 0:
            continue
        di_plus = 100.0 * plus / tr
        di_minus = 100.0 * minus / tr
        dx_values.append(100.0 * abs(di_plus - di_minus) / (di_plus + di_minus))
    if len(dx_values) < period:
        raise ValueError("not_enough_bars_for_adx")
    adx_values = _wilders_smooth(dx_values, period)
    return adx_values[-1]


def rsi(bars: list[dict[str, float]], period: int = 14) -> float:
    """Wilder 相对强弱指数（RSI），取最后一根已收盘 K 线的值。

    收盘全涨时返回 100.0（平均亏损为 0）。需要至少 period+2 根 K 线。
    """
    if len(bars) < period + 2:
        raise ValueError("not_enough_bars_for_rsi")
    closes = [bar["close"] for bar in bars]
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def stddev(bars: list[dict[str, float]], period: int = 20) -> float:
    """最近 period 根已收盘 K 线收盘价的总体标准差（波动确认用）。"""
    if len(bars) < period:
        raise ValueError("not_enough_bars_for_stddev")
    closes = [bar["close"] for bar in bars[-period:]]
    mean = sum(closes) / period
    return (sum((close - mean) ** 2 for close in closes) / period) ** 0.5


def ema(bars: list[dict[str, float]], period: int) -> float:
    """指数移动平均（EMA），取最后一根已收盘 K 线的值。

    首值取前 period 根收盘价的简单平均，其后按 k = 2/(period+1) 递推。
    需要至少 period 根 K 线。
    """
    if len(bars) < period:
        raise ValueError("not_enough_bars_for_ema")
    closes = [bar["close"] for bar in bars]
    multiplier = 2.0 / (period + 1)
    value = sum(closes[:period]) / period
    for close in closes[period:]:
        value = (close - value) * multiplier + value
    return value


def cci(bars: list[dict[str, float]], period: int = 14) -> float:
    """商品通道指数（CCI），取最后一根已收盘 K 线的值。

    典型价格 = (高+低+收)/3；CCI = (tp - SMA(tp)) / (0.015 * 平均绝对偏差)。
    需要至少 period 根 K 线。恒鑫 EA 精华：CCI ±100 为上下轨，轨外不开首单。
    """
    if len(bars) < period:
        raise ValueError("not_enough_bars_for_cci")
    typical = [(bar["high"] + bar["low"] + bar["close"]) / 3.0 for bar in bars]
    window = typical[-period:]
    mean = sum(window) / period
    deviations = [abs(value - mean) for value in window]
    mean_deviation = sum(deviations) / period
    if mean_deviation == 0:
        return 0.0
    return (typical[-1] - mean) / (0.015 * mean_deviation)


def macd_series(
    bars: list[dict[str, float]],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, float]:
    """MACD 三要素（最后一根已收盘 K 线）：快线 / 信号线 / 柱值。

    需要至少 slow + signal 根 K 线。用于背离检测（macd_divergence）与
    军师简报的 MACD 状态维度。返回 {macd, signal, histogram}。
    """
    if len(bars) < slow + signal:
        raise ValueError("not_enough_bars_for_macd")
    closes = [bar["close"] for bar in bars]
    fast_multiplier = 2.0 / (fast + 1)
    slow_multiplier = 2.0 / (slow + 1)
    fast_ema = sum(closes[:fast]) / fast
    slow_ema = sum(closes[:slow]) / slow
    # 信号线 = macd 线的 EMA(9)：从 slow 根之后逐根递推，需要额外补齐窗口。
    macd_values: list[float] = []
    for index in range(slow, len(closes)):
        fast_ema = (closes[index] - fast_ema) * fast_multiplier + fast_ema
        slow_ema = (closes[index] - slow_ema) * slow_multiplier + slow_ema
        macd_values.append(fast_ema - slow_ema)
    signal_ema = sum(macd_values[:signal]) / signal
    for value in macd_values[signal:]:
        signal_ema = (value - signal_ema) * (2.0 / (signal + 1)) + signal_ema
    macd = macd_values[-1]
    signal_value = signal_ema
    return {
        "macd": round(macd, 6),
        "signal": round(signal_value, 6),
        "histogram": round(macd - signal_value, 6),
    }


def normalize_rates(rates: Any) -> list[dict[str, float]]:
    rows = [
        {
            "time": int(row["time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for row in rates
    ]
    return sorted(rows, key=lambda row: row["time"])


def resolve_symbol(mt5: Any, preferred: str) -> str:
    if mt5.symbol_info(preferred) is not None:
        return preferred
    candidates = []
    for info in mt5.symbols_get("*XAU*") or []:
        name = getattr(info, "name", "")
        if "XAU" in name.upper() and ("USD" in name.upper() or name.upper() == "GOLD"):
            candidates.append(name)
    candidates = sorted(set(candidates), key=len)
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(f"set MT5_SYMBOL explicitly; candidates={candidates[:10]}")


def import_mt5() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:
        raise SystemExit("MetaTrader5 Python package missing. Run: python -m pip install MetaTrader5") from exc
    return mt5


def session_label(bar_time: int | None) -> str | None:
    if bar_time is None:
        return None
    hour = datetime.fromtimestamp(int(bar_time), timezone.utc).hour
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "london_ny_overlap"
    if 16 <= hour < 21:
        return "ny_late"
    if 21 <= hour < 22:
        return "rollover"
    return "off_hours"
