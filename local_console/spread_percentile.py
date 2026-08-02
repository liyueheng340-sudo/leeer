"""Spread-history percentile for the XAU facts layer (A3).

点差历史分位：当前任务的 tick_health.spread_median 相对近 N 次已完成任务
的分位数（0-1）。用途：判断"当前点差是该经纪商该时段的常态还是异常"，
比绝对阈值（guard.SPREAD_DOWNGRADE_THRESHOLD）更贴近常态。

数据来源：jobs 目录已落盘任务 JSON 的 gate.tick_health.spread_median。
- 历史样本不足（<SPREAD_PERCENTILE_MIN_SAMPLES）时返回 None（如实标注积累中）；
- 读取任何异常都静默返回 None（失败安全，绝不阻断任务）。
"""

from __future__ import annotations

from .jobs import JobStore

# 分位计算需要的最少历史样本数（含当前）。
SPREAD_PERCENTILE_MIN_SAMPLES = 5
# 参与分位的历史任务上限（防止目录膨胀拖慢每次任务）。
SPREAD_PERCENTILE_HISTORY_LIMIT = 60


def _historical_spreads(store: JobStore, limit: int) -> list[float]:
    """取最近 limit 个已完成任务的 spread_median 列表（忽略无值/损坏记录）。"""
    values: list[float] = []
    try:
        records = store.list_recent(limit=limit)
    except Exception:
        return values
    for record in records:
        gate = record.gate
        if not isinstance(gate, dict):
            continue
        tick = gate.get("tick_health")
        if not isinstance(tick, dict) or tick.get("available") is not True:
            continue
        median = tick.get("spread_median")
        if isinstance(median, (int, float)) and median > 0:
            values.append(float(median))
    return values


def compute_spread_percentile(
    current_median: float | None, store: JobStore
) -> float | None:
    """当前 spread_median 在近 N 次任务中的分位（0-1）；样本不足或异常返回 None。

    分位 = 历史中 <= 当前值的比例（同 IV Rank 的百分位口径，低分位 = 当前点差偏小）。
    store 复用调用方持有的 JobStore（同一把锁），不新建实例。
    """
    if not isinstance(current_median, (int, float)) or current_median <= 0:
        return None
    try:
        history = _historical_spreads(store, SPREAD_PERCENTILE_HISTORY_LIMIT)
    except Exception:
        return None
    if len(history) < SPREAD_PERCENTILE_MIN_SAMPLES:
        return None  # 样本不足：如实标注"数据积累中"，不虚构分位
    below = sum(1 for value in history if value <= current_median)
    return round(below / len(history), 3)
