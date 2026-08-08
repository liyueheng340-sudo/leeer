"""Deterministic fractal breakout levels from closed D1 bars (Gold Trade Pro essence).

Gold Trade Pro v1.31 的入场核心（ccbsw_10/11）翻译为确定性事实：
- 在日线上找分形高点：左边 N 根 K 线内最高 + 右边 N 根 K 线内最高（左右对称验证）；
- 分形位必须与现价保持最小突破距离（min_distance_points），过滤假突破；
- 输出上方 BUYSTOP 参考位 / 下方 SELLSTOP 参考位，供军师简报引用。

全部由已收盘 K 线复算，非模型推断——与 resonance.py / regime.py 同一套
"确定性事实"哲学，维度是"结构入场位"而非方向或状态。
"""

from __future__ import annotations

# Gold Trade Pro 默认分形左右验证根数（原 EA 可配置：left=2, right=4~18）。
FRACTAL_LEFT_BARS = 2
FRACTAL_RIGHT_BARS = 2
# 最小突破距离（以价格为单位）：分形位离现价不足该距离视为"假突破位"，跳过。
# 对 XAUUSD 取 2 美元（约 200 点），对应原 EA 的 150~900 点区间下沿。
MIN_BREAKOUT_DISTANCE = 2.0
# 分形位距现价超过该倍数 ATR 时不再挂单（原 EA 用 bar 数扫描上限，这里用
# ATR 归一化距离，避免静态 bar 数在低波动日误判）。
MAX_SCAN_ATR_DISTANCE = 6.0


def _is_fractal_high(bars: list[dict[str, float]], index: int, *, left: int, right: int) -> bool:
    """bars[index] 是否为分形高点：左边 left 根与右边 right 根均不高于它。"""
    if index < left or index + right >= len(bars):
        return False
    value = float(bars[index]["high"])
    return all(float(bars[index - offset]["high"]) <= value for offset in range(1, left + 1)) and all(
        float(bars[index + offset]["high"]) <= value for offset in range(1, right + 1)
    )


def _is_fractal_low(bars: list[dict[str, float]], index: int, *, left: int, right: int) -> bool:
    """bars[index] 是否为分形低点：左边 left 根与右边 right 根均不低于它。"""
    if index < left or index + right >= len(bars):
        return False
    value = float(bars[index]["low"])
    return all(float(bars[index - offset]["low"]) >= value for offset in range(1, left + 1)) and all(
        float(bars[index + offset]["low"]) >= value for offset in range(1, right + 1)
    )


def compute_fractal_levels(snapshot: dict[str, object]) -> dict[str, object]:
    """从快照的 d1_bars 计算分形突破位事实。

    返回结构（available=True 时）：
        buy_levels    有效分形高点（上方突破参考位，价格升序）
        sell_levels   有效分形低点（下方突破参考位，价格降序）
        nearest_buy   距现价最近的上方分形位（None 表示无）
        nearest_sell  距现价最近的下方分形位（None 表示无）
        reference_atr 用于归一化的 D1 ATR（None 表示数据不足）
        note          确定性事实说明
    """
    bars = snapshot.get("d1_bars")
    if not isinstance(bars, list) or len(bars) < FRACTAL_LEFT_BARS + FRACTAL_RIGHT_BARS + 3:
        return {"available": False, "reason": "快照缺少 d1_bars"}

    bid = snapshot.get("bid")
    ask = snapshot.get("ask")
    price = None
    if isinstance(bid, (int, float)) and bid > 0:
        price = float(bid)
    elif isinstance(ask, (int, float)) and ask > 0:
        price = float(ask)
    if price is None:
        return {"available": False, "reason": "快照缺少现价"}

    # D1 ATR（14）用于归一化距离与提示波动尺度。
    atr_value: float | None = None
    if len(bars) >= 15:
        true_ranges: list[float] = []
        for index in range(len(bars) - 14, len(bars)):
            previous_close = float(bars[index - 1]["close"])
            high = float(bars[index]["high"])
            low = float(bars[index]["low"])
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        atr_value = sum(true_ranges) / 14

    buy_levels: list[float] = []
    sell_levels: list[float] = []
    max_distance = (atr_value or 0.0) * MAX_SCAN_ATR_DISTANCE
    for index in range(len(bars)):
        # 只取已确认的分形（右侧验证窗口全部收盘）。
        if index + FRACTAL_RIGHT_BARS >= len(bars) - 1:
            continue
        if _is_fractal_high(bars, index, left=FRACTAL_LEFT_BARS, right=FRACTAL_RIGHT_BARS):
            level = float(bars[index]["high"])
            distance = level - price
            if distance >= MIN_BREAKOUT_DISTANCE and (max_distance <= 0 or distance <= max_distance):
                buy_levels.append(round(level, 2))
        if _is_fractal_low(bars, index, left=FRACTAL_LEFT_BARS, right=FRACTAL_RIGHT_BARS):
            level = float(bars[index]["low"])
            distance = price - level
            if distance >= MIN_BREAKOUT_DISTANCE and (max_distance <= 0 or distance <= max_distance):
                sell_levels.append(round(level, 2))

    buy_levels = sorted(set(buy_levels))
    sell_levels = sorted(set(sell_levels), reverse=True)

    return {
        "available": True,
        "buy_levels": buy_levels[:5],
        "sell_levels": sell_levels[:5],
        "nearest_buy": buy_levels[0] if buy_levels else None,
        "nearest_sell": sell_levels[0] if sell_levels else None,
        "reference_atr": round(atr_value, 6) if atr_value else None,
        "note": (
            "确定性事实：由已收盘日线分形高低点复算的突破参考位"
            "（Gold Trade Pro 精华），非模型推断。"
        ),
    }
