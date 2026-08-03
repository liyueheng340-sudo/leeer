"""Event-calendar source parsing: JSON / ForexFactory XML / ICS / Finviz → normalized events."""

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

# Finviz 内嵌 JSON blob 起点标记（calendar.ashx 返回 HTML，内嵌 {"initialDateFrom":...,"entries":[...]}）
FINVIZ_BLOB_START = '{"initialDateFrom"'
# 只保留 importance >= 2（finviz 1-3 等级；1 为低影响杂项，不值得进闸门日历）
FINVIZ_MIN_IMPORTANCE = 2
# 防恶意超长响应：内嵌 JSON 超过该字节即拒绝解析
FINVIZ_MAX_BLOB_BYTES = 4 * 1024 * 1024


def events_from_json(text: str) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return None
    return [entry for entry in payload["events"] if _valid_event(entry)]


def events_from_finviz(text: str) -> list[dict[str, Any]]:
    """Parse the Finviz economic calendar page (calendar.ashx).

    Finviz 是美国宏观日历：HTML 页面内嵌一个 JSON blob
    （{"initialDateFrom":..., "entries":[{date, event, importance, ticker, ...}]}），
    服务端渲染，无需 JS，无 API key，实测稳定（本网络 6/6 请求 200）。
    从稳定源读取时 parse_calendar_payload 通过 FINVIZ_BLOB_START 识别并转发至此。

    importance 映射：3 → high（对应非农/CPI/FOMC 级别）、2 → medium、
    1 → low（丢弃）。标题加"美国·"前缀（finviz 即美国宏观日历），
    时间字段 date 为 ISO 格式（含偏移），由 _parse_instant 统一转 UTC。
    """
    start = text.find(FINVIZ_BLOB_START)
    if start < 0:
        return []
    # 括号配对提取完整 JSON（防截断/夹带）
    depth = 0
    end = start
    for i in range(start, min(start + FINVIZ_MAX_BLOB_BYTES, len(text))):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if depth != 0:
        return []  # blob 未闭合（超长或损坏）
    try:
        payload = json.loads(text[start:end])
    except ValueError:
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    events: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        importance = entry.get("importance")
        if not isinstance(importance, int) or importance < FINVIZ_MIN_IMPORTANCE:
            continue
        title = entry.get("event")
        if not isinstance(title, str) or not title.strip():
            continue
        impact = "high" if importance >= 3 else "medium"
        instant = _finviz_instant(entry.get("date"))
        if instant is None:
            continue
        events.append(
            {
                "title": f"美国·{title.strip()}",
                "utc": instant.isoformat(),
                "impact": impact,
            }
        )
    return events


def _finviz_instant(value: object) -> datetime | None:
    """Finviz date 字段（'2026-08-03T10:00:00'）按美国东部时间解释，转 UTC。

    finviz 的 date 是本地时间（无偏移，实际为 ET）；若不解释直接当 UTC，
    事件时刻会差 4-5 小时（非农 08:30 ET 会被当成 08:30 UTC，整整晚 4 小时）。
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        local = datetime.fromisoformat(value)
    except ValueError:
        return None
    if local.tzinfo is not None:
        return local.astimezone(UTC)  # 若源将来带偏移，直接转
    return local.replace(tzinfo=US_EASTERN).astimezone(UTC)


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
    """Detect payload format (JSON / ICS / FF XML / Finviz) and return normalized events."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return events_from_json(text)
    if "BEGIN:VCALENDAR" in text:
        return events_from_ics(text)
    if FINVIZ_BLOB_START in text:
        return events_from_finviz(text)
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
