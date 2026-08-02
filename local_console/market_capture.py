"""Sensor capture helpers: MT5 market data + safe sensor wrappers.

传感器与宏观/新闻/EA 背景层都是"辅助降级"读取：任何失败都静默降级为
不可用/无背景，绝不阻断任务（与 guard.py 只降级不阻断原则一致）。
"""

from __future__ import annotations

from collections.abc import Callable

from .config import ConsoleConfig
from .snapshot import SnapshotCaptureError, capture_combined

SnapshotRunner = Callable[[ConsoleConfig, str], dict[str, object]]
TickRunner = Callable[[ConsoleConfig, str], dict[str, object]]
MacroRunner = Callable[[ConsoleConfig], dict[str, object]]
NewsRunner = Callable[[ConsoleConfig], dict[str, object]]
EaStatusRunner = Callable[[ConsoleConfig], dict[str, object]]
MarketDataRunner = Callable[[ConsoleConfig, str], tuple[dict[str, object], dict[str, object]]]


def capture_market_data(
    config: ConsoleConfig,
    job_id: str,
    *,
    market_data_runner: MarketDataRunner | None,
    snapshot_runner: SnapshotRunner,
    tick_runner: TickRunner,
) -> tuple[dict[str, object], dict[str, object]]:
    """一次 MT5 会话产出快照与 tick 健康；合并采集失败时回退到两个独立脚本。"""
    if market_data_runner is not None:
        return market_data_runner(config, job_id)
    try:
        return capture_combined(config, job_id)
    except SnapshotCaptureError:
        snapshot = snapshot_runner(config, job_id)
        tick_health = safe_tick_health(config, job_id, tick_runner)
        return snapshot, tick_health


def safe_tick_health(
    config: ConsoleConfig, job_id: str, tick_runner: TickRunner
) -> dict[str, object]:
    """tick 传感器是辅助降级触发器，故障时静默降级为不可用，绝不阻断任务。"""
    try:
        return tick_runner(config, job_id)
    except Exception:
        return {"available": False, "reason": "tick 传感器异常"}


def safe_macro(config: ConsoleConfig, macro_runner: MacroRunner) -> dict[str, object]:
    """宏观背景层同理：不可用只是少一层背景，不是任务失败。"""
    try:
        return macro_runner(config)
    except Exception:
        return {"status": "unavailable", "reason": "宏观背景获取异常"}


def safe_news(config: ConsoleConfig, news_runner: NewsRunner) -> dict[str, object]:
    """新闻背景层同理：不可用只是少一层预期差参考，不是任务失败。"""
    try:
        return news_runner(config)
    except Exception:
        return {"status": "unavailable", "reason": "新闻背景获取异常"}


def safe_ea_status(
    config: ConsoleConfig, ea_status_runner: EaStatusRunner
) -> dict[str, object]:
    """EA 风控态与 tick 传感器同级：辅助降级触发器，故障时静默不可用，绝不阻断。"""
    try:
        return ea_status_runner(config)
    except Exception:
        return {"available": False, "reason": "EA 状态读取异常"}
