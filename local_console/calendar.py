"""Automated high-impact event calendar for the XAU fact gate.

事件上下文解析顺序：
1. 手工覆写优先——event_context.json 存在且合法时直接使用（兼容旧行为）；
2. 否则（按 3 小时节流）从日历源拉取并缓存到 calendar.json：
   - XAU_CONSOLE_CALENDAR_URL 指向的 JSON / ICS / ForexFactory 周历 XML；
   - 未设置时默认使用 ForexFactory 公开周历 XML（仅取 USD 高/中影响，
     时间从美东换算 UTC）。拉取失败自动回退本地文件；
3. 评估 calendar.json：高影响事件前 30 分钟至后 15 分钟 → WAIT。

calendar.json 格式：
{
  "updated_at": "2026-07-30T00:00:00+00:00",
  "events": [
    {"title": "美国核心 PCE", "utc": "2026-07-31T12:30:00+00:00", "impact": "high"}
  ]
}

日历超过 CALENDAR_FRESHNESS_HOURS 未更新即视为不可信（unverified → WATCH），
与系统"先验证，后分析"的保守取向一致。
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from .config import ConsoleConfig
from .guard import load_event_context as load_manual_override

CALENDAR_FRESHNESS_HOURS = 36
FETCH_THROTTLE_HOURS = 3
WINDOW_BEFORE_MINUTES = 30
WINDOW_AFTER_MINUTES = 15
FETCH_TIMEOUT_SECONDS = 10
CALENDAR_URL_ENV = "XAU_CONSOLE_CALENDAR_URL"
# 免费公开周历（ForexFactory/faireconomy），仅取 USD 事件
DEFAULT_CALENDAR_URL = "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml"
US_EASTERN = ZoneInfo("America/New_York")

# ICS 源没有 impact 字段时的关键词兜底（命中才记为 high）
HIGH_IMPACT_KEYWORDS = re.compile(
    r"NFP|Nonfarm|CPI|PCE|GDP|FOMC|Rate Decision|Payroll|CPI|PPI|Retail Sales|Jackson Hole",
    re.IGNORECASE,
)


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _valid_event(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("title"), str)
        and bool(entry["title"].strip())
        and _parse_instant(entry.get("utc")) is not None
    )


def _load_calendar_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return None
    return payload


# ─── 日历源解析：JSON / ICS / ForexFactory XML ───

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


def refresh_calendar_from_url(config: ConsoleConfig) -> bool:
    """Throttled best-effort refresh of calendar.json from the configured/default URL."""
    url = os.environ.get(CALENDAR_URL_ENV) or DEFAULT_CALENDAR_URL
    try:
        mtime = datetime.fromtimestamp(config.calendar_path.stat().st_mtime, UTC)
        if datetime.now(UTC) - mtime < timedelta(hours=FETCH_THROTTLE_HOURS):
            return False  # 3 小时内拉过，不重复请求
    except OSError:
        pass  # 文件不存在 → 立即拉取
    try:
        with urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    events = parse_calendar_payload(text)
    if not events:
        return False
    payload = {
        "updated_at": datetime.now(UTC).isoformat(),
        "source": url,
        "events": events,
    }
    try:
        config.calendar_path.parent.mkdir(parents=True, exist_ok=True)
        config.calendar_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        return False
    return True


def evaluate_calendar(path: Path, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate the curated calendar into the gate's event-context shape."""
    reference_now = (now or datetime.now(UTC)).astimezone(UTC)
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
