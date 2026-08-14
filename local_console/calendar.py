"""Automated high-impact event calendar for the XAU fact gate.

事件上下文解析顺序：
1. 手工覆写优先——event_context.json 存在且合法时直接使用（兼容旧行为）；
2. 否则（按 3 小时节流）从日历源拉取并缓存到 calendar.json：
   - XAU_CONSOLE_CALENDAR_URL 指向的 JSON / ICS / ForexFactory 周历 XML；
   - 未设置时默认使用 ForexFactory 公开周历 XML（仅取 USD 高/中影响，
     时间从美东换算 UTC）。拉取失败自动回退本地文件；
3. 评估 calendar.json：高影响事件前 60 分钟至后 30 分钟 → wait 状态
   （guard 在军师模式下将其转为风险标注，不锁模型）。

calendar.json 格式（由 refresh_calendar_from_url 生成，source/schema_version
为代码生成标记，缺失即视为不可信的手工文件）：
{
  "updated_at": "2026-07-30T00:00:00+00:00",
  "source": "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml",
  "schema_version": 1,
  "events": [
    {"title": "美国核心 PCE", "utc": "2026-07-31T12:30:00+00:00", "impact": "high"}
  ]
}

可信度防线（杜绝手工/错误数据冒充）：
- source 标记缺失 → unverified（拒绝使用，绝不当作已核验事件）；
- 高影响事件落在周末（周六/周日）→ unverified——美国宏观数据（非农/CPI/
  PCE/FOMC 等）从不在周末发布，周末出现 high 事件必然是日期写错；
- 日历超过 CALENDAR_FRESHNESS_HOURS 未更新即视为不可信（unverified），
  事件驱动信息随报告标注"未核验"，不阻断分析（宪法：数据保真，永不锁死）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .calendar_parsers import (
    CALENDAR_SCHEMA_VERSION,
    _parse_instant,
    _valid_event,
    events_from_ff_xml,
    events_from_ics,
    events_from_json,
    parse_calendar_payload,
)
from .calendar_refresh import (
    CALENDAR_MAX_BYTES,
    CALENDAR_URL_ENV,
    DEFAULT_CALENDAR_URL,
    FETCH_THROTTLE_HOURS,
    FETCH_TIMEOUT_SECONDS,
    refresh_calendar_from_url,
)
from .config import ConsoleConfig
from .guard import load_event_context as load_manual_override

CALENDAR_FRESHNESS_HOURS = 36
# 事件禁开窗（对齐响马 set 的新闻过滤纪律：前 120/后 60 的折中）：
# 高影响事件前 60 分钟至后 30 分钟 → wait 状态；guard 在军师模式下
# 将其转为风险标注随报告呈现，不锁模型。
WINDOW_BEFORE_MINUTES = 60
WINDOW_AFTER_MINUTES = 30

__all__ = [
    "CALENDAR_FRESHNESS_HOURS",
    "CALENDAR_MAX_BYTES",
    "CALENDAR_SCHEMA_VERSION",
    "CALENDAR_URL_ENV",
    "DEFAULT_CALENDAR_URL",
    "FETCH_THROTTLE_HOURS",
    "FETCH_TIMEOUT_SECONDS",
    "WINDOW_AFTER_MINUTES",
    "WINDOW_BEFORE_MINUTES",
    "evaluate_calendar",
    "events_from_ff_xml",
    "events_from_ics",
    "events_from_json",
    "load_event_context",
    "parse_calendar_payload",
    "refresh_calendar_from_url",
]


def _load_calendar_file(path: Path) -> dict[str, Any] | None:
    """Load the cached calendar, requiring the code-generation trust marker.

    只信任 refresh_calendar_from_url 写出的文件：必须带 source 与
    schema_version 标记，否则视为手工残留文件（不可信）返回 None。
    """
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return None
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip():
        return None
    if payload.get("schema_version") != CALENDAR_SCHEMA_VERSION:
        return None
    return payload


def evaluate_calendar(path: Path, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate the curated calendar into the gate's event-context shape."""
    reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = _load_calendar_file(path)
    if payload is None:
        return {"status": "unverified", "reason": "事件日历缺失或无法解析"}
    updated_at = _parse_instant(payload.get("updated_at"))
    if updated_at is None or reference_now - updated_at > timedelta(
        hours=CALENDAR_FRESHNESS_HOURS
    ):
        return {"status": "unverified", "reason": "事件日历已过期，需更新"}

    events = [entry for entry in payload["events"] if _valid_event(entry)]
    high_impact = [
        entry for entry in events if str(entry.get("impact", "")).lower() == "high"
    ]
    # 数据合理性防线：美国高影响数据（非农/CPI/PCE/FOMC…）从不在周末发布。
    # 周末出现 high 事件 = 日期数据错误（典型：手工文件把 8/7 写成 8/1），
    # 此时绝不当作已核验事件，而是判 unverified 让 guard 标注"未核验"。
    for entry in high_impact:
        instant = _parse_instant(entry["utc"])
        if instant is not None and instant.weekday() >= 5:
            return {
                "status": "unverified",
                "reason": f"高影响事件日期不合法（周末）：{entry['title']}",
                "event": {
                    "title": entry["title"],
                    "utc": instant.isoformat(),
                },
            }
    for entry in high_impact:
        instant = _parse_instant(entry["utc"])
        window_start = instant - timedelta(minutes=WINDOW_BEFORE_MINUTES)
        window_end = instant + timedelta(minutes=WINDOW_AFTER_MINUTES)
        if window_start <= reference_now <= window_end:
            return {
                "status": "wait",
                "reason": f"高影响事件窗口：{entry['title']}",
                "event": {
                    "title": entry["title"],
                    "utc": instant.isoformat(),
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                },
            }

    future = [
        (entry, _parse_instant(entry["utc"]))
        for entry in high_impact
        if _parse_instant(entry["utc"]) > reference_now
    ]
    # 已过去的高影响事件（最近 48 小时内）：让模型知道哪些数据已经公布，
    # 避免日历日期偏差导致模型误报"即将公布"。
    past_cutoff = reference_now - timedelta(hours=48)
    past = [
        {"title": entry["title"], "utc": instant.isoformat()}
        for entry in high_impact
        if (instant := _parse_instant(entry["utc"])) is not None
        and past_cutoff < instant <= reference_now
    ]
    result: dict[str, Any] = {
        "status": "verified_clear",
        "reason": "事件日历已核验，当前无高影响事件窗口",
        "current_utc": reference_now.isoformat(),
    }
    if future:
        entry, instant = min(future, key=lambda pair: pair[1])
        result["next_event"] = {"title": entry["title"], "utc": instant.isoformat()}
    if past:
        result["past_events"] = past
    return result


def load_event_context(
    config: ConsoleConfig, now: datetime | None = None
) -> dict[str, Any]:
    """Manual override wins; otherwise evaluate the (optionally refreshed) calendar."""
    manual = load_manual_override(config.event_context_path)
    if manual.get("status") in {"verified_clear", "wait"}:
        return manual
    refresh_calendar_from_url(config)
    return evaluate_calendar(config.calendar_path, now)
