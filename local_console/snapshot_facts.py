"""Shared deterministic fact extraction from the snapshot dict.

全部提取都是"已提供事实 → 确定性数值"的纯函数，无模型推断：
- 参考 ATR（H1 优先，其次 M15/H4，最后 atr_m15 兜底）；
- 关键价位层（前日高低/当日高低/最近整数关口/最近摆动点）；
- 自由文本价格解析（'4070-4078' → [4070.0, 4078.0]）。

prompt_rules 与 report_validation 共同消费，保持单一实现。
"""

from __future__ import annotations

import re

NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
# 关键价位判定：入场区间中点需落在某关键价位 ± 1.0 ATR 内（剥头皮 M1，取 H1 ATR 作尺度）。
KEY_LEVEL_ATR_TOLERANCE = 1.0


def _parse_prices(value: object) -> list[float]:
    """Extract numeric price levels from a free-form string ('4070-4078' -> [4070.0, 4078.0])."""
    if not isinstance(value, str):
        return []
    return [float(token) for token in NUMBER_PATTERN.findall(value)]


def _reference_atr(snapshot: dict[str, object]) -> float | None:
    structure = snapshot.get("timeframe_structure")
    if isinstance(structure, dict):
        for timeframe in ("h1", "m15", "h4"):
            frame = structure.get(timeframe)
            if isinstance(frame, dict) and isinstance(frame.get("atr_14"), (int, float)):
                return float(frame["atr_14"])
    fallback = snapshot.get("atr_m15")
    if isinstance(fallback, (int, float)):
        return float(fallback)
    return None


def _key_levels(snapshot: dict[str, object]) -> list[float]:
    """从快照确定性提取关键价位：前日高/低、当日高/低、最近整数关口、最近摆动点。

    全部来自已提供事实（latest_closed_bars / timeframe_structure 的最近已收盘 K 线），
    无模型推断；任一数据缺失即跳过该项。返回去重升序价位列表。
    """
    levels: list[float] = []
    bars = snapshot.get("latest_closed_bars")
    if isinstance(bars, dict):
        for tf in ("h4", "h1", "m15", "m5"):
            bar = bars.get(tf)
            if isinstance(bar, dict):
                high, low = bar.get("high"), bar.get("low")
                if isinstance(high, (int, float)):
                    levels.append(float(high))
                if isinstance(low, (int, float)):
                    levels.append(float(low))
    structure = snapshot.get("timeframe_structure")
    if isinstance(structure, dict):
        for tf in ("h1", "m15", "m5"):
            frame = structure.get(tf)
            if isinstance(frame, dict):
                high, low = frame.get("high"), frame.get("low")
                if isinstance(high, (int, float)):
                    levels.append(float(high))
                if isinstance(low, (int, float)):
                    levels.append(float(low))
    bid = snapshot.get("bid")
    if isinstance(bid, (int, float)) and bid > 0:
        for increment in (50, 100):
            base = round(float(bid) / increment) * increment
            levels.extend([base - increment, base, base + increment])
    unique = sorted({round(level, 2) for level in levels if level > 0})
    return unique[:16]  # 防 prompt 膨胀，最多 16 个
