"""只读消费 Cerberus EA 的运行态文件 ng_status.json（接口 B：闸门前向对齐）。

纪律（HY3 口径，与 docs/cerberus-ea-interface-map.md 一致）：
- 只消费风险机制字段：status / regime_blocked / hour.blocked / feed / gmt。
- 绝不读取持仓、篮子、盈亏等事后测量——它们不构成预测证据，不进入闸门。
- 失败安全：文件缺失、不可解析、陈旧或字段异常一律视为不可用并静默忽略，
  与 guard.py "只降级、不阻断" 原则一致；EA 未运行时控制台行为与接入前完全相同。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ConsoleConfig

# ng_status.json 的 gmt 字段由 MT5 TimeToString(TIME_DATE|TIME_SECONDS) 生成，
# 形如 "2026.07.24 11:30:23"，语义为 UTC。
GMT_FORMAT = "%Y.%m.%d %H:%M:%S"


def read_ea_status(
    path: Path | None,
    now: datetime,
    max_age_seconds: float = 120.0,
) -> dict[str, object]:
    """读取并归一化 EA 运行态；任何异常都返回 available=False，绝不抛出。

    max_age_seconds 默认 120 秒：EA 运行时约每 30 秒写一次（OnTimer 节流），
    4 倍余量覆盖抖动；超过即视为 EA 已停写，状态不可用。
    """
    if path is None:
        return {"available": False, "reason": "未配置 EA 状态文件路径"}
    try:
        if not path.is_file():
            return {"available": False, "reason": "EA 状态文件不存在（EA 未运行或未写入）"}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"available": False, "reason": "EA 状态文件不可解析"}
    if not isinstance(payload, dict):
        return {"available": False, "reason": "EA 状态文件结构异常"}

    gmt_raw = payload.get("gmt")
    if not isinstance(gmt_raw, str):
        return {"available": False, "reason": "EA 状态缺少 gmt 时间戳"}
    try:
        captured = datetime.strptime(gmt_raw, GMT_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return {"available": False, "reason": "EA 状态时间戳格式异常"}

    age = max(0.0, (now.astimezone(UTC) - captured).total_seconds())
    if age > max_age_seconds:
        return {
            "available": False,
            "reason": f"EA 状态已陈旧（{age:.0f} 秒前写入），按不可用处理",
            "age_seconds": age,
        }

    hour = payload.get("hour")
    return {
        "available": True,
        "status": payload.get("status"),
        "regime_blocked": payload.get("regime_blocked") is True,
        "hour_blocked": isinstance(hour, dict) and hour.get("blocked") is True,
        "feed": payload.get("feed"),
        "gmt": gmt_raw,
        "age_seconds": age,
    }


def load_ea_status(config: ConsoleConfig) -> dict[str, object]:
    """按控制台配置读取 EA 运行态（服务层默认 runner）。"""
    return read_ea_status(
        config.ea_status_path,
        datetime.now(UTC),
        config.ea_status_max_age_seconds,
    )
