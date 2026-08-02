"""Event-calendar source parsing: JSON / ForexFactory XML / ICS → normalized events."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

# 免费公开周历（ForexFactory/faireconomy），仅取 USD 事件
US_EASTERN = ZoneInfo("America/New_York")

# 缓存文件代码生成标记：refresh 写入时带上；加载时缺失即视为手工/残留文件
# （不可信），防止错误的日历数据被当作已核验来源。
CALENDAR_SCHEMA_VERSION = 1

# ICS 源没有 impact 字段时的关键词兜底（命中才记为 high）
HIGH_IMPACT_KEYWORDS = re.compile(
    r"NFP|Nonfarm|CPI|PCE|GDP|FOMC|Rate Decision|Payroll|CPI|PPI|Retail Sales|Jackson Hole",
    re.IGNORECASE,
)


def events_from_json(text: str) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return None
    return [entry for entry in payload["events"] if _valid_event(entry)]


def events_from_ff_xml(text: str) -> list[dict[str, Any]]:
    """Parse the ForexFactory weekly XML; keep USD events, convert ET → UTC."""
    events: list[dict[str, Any]] = []
    # 实体扩展防线：拒绝带 DOCTYPE/ENTITY 的 XML（billion-laughs 攻击面）。
    head = text[:4096].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        return events
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return events
    for node in root.iter("event"):
        country = (node.findtext("country") or "").strip().upper()
        if country != "USD":
            continue  # XAU 盘面只跟美元高影响事件
        title = (node.findtext("title") or "").strip()
        date_text = (node.findtext("date") or "").strip()
        time_text = (node.findtext("time") or "").strip().lower()
        impact = (node.findtext("impact") or "").strip().lower()
        if not title or impact not in ("high", "medium", "low"):
            continue
        try:
            day = datetime.strptime(date_text, "%m-%d-%Y")
            moment = datetime.strptime(time_text, "%I:%M%p")
        except ValueError:
            continue  # Tentative / All Day 等无确定时刻的条目跳过
        local = day.replace(hour=moment.hour, minute=moment.minute, tzinfo=US_EASTERN)
        events.append(
            {
                "title": f"美国·{title}",
                "utc": local.astimezone(UTC).isoformat(),
                "impact": impact,
            }
        )
    return events


def _unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_ics_datetime(value: str, tzid: str | None) -> datetime | None:
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            moment = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt.endswith("Z"):
            return moment.replace(tzinfo=UTC)
        if tzid:
            try:
                return moment.replace(tzinfo=ZoneInfo(tzid)).astimezone(UTC)
            except (KeyError, ValueError):
                return None
        return moment.replace(tzinfo=UTC)  # 浮动时间按 UTC 处理（文档化限制）
    return None


def events_from_ics(text: str) -> list[dict[str, Any]]:
    """Parse a VCALENDAR feed; keyword-match SUMMARY to assign high impact."""
    events: list[dict[str, Any]] = []
    lines = _unfold_ics(text)
    in_event = False
    summary: str | None = None
    dtstart: datetime | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event, summary, dtstart = True, None, None
            continue
        if line == "END:VEVENT":
            if in_event and summary and dtstart is not None:
                impact = "high" if HIGH_IMPACT_KEYWORDS.search(summary) else "low"
                events.append({"title": summary, "utc": dtstart.isoformat(), "impact": impact})
            in_event = False
            continue
        if not in_event or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, _, params = head.partition(";")
        if name == "SUMMARY":
            summary = value.strip()
        elif name == "DTSTART":
            tzid = None
            match = re.search(r"TZID=([^;:]+)", params)
            if match:
                tzid = match.group(1)
            dtstart = _parse_ics_datetime(value, tzid)
    return events


def parse_calendar_payload(text: str) -> list[dict[str, Any]] | None:
    """Detect payload format (JSON / ICS / FF XML) and return normalized events."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return events_from_json(text)
    if "BEGIN:VCALENDAR" in text:
        return events_from_ics(text)
    if stripped.startswith("<"):
        return events_from_ff_xml(text)
    return None


def _valid_event(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("title"), str)
        and bool(entry["title"].strip())
        and _parse_instant(entry.get("utc")) is not None
    )


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None
