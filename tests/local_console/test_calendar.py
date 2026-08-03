from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from local_console.calendar import (
    WINDOW_AFTER_MINUTES,
    WINDOW_BEFORE_MINUTES,
    evaluate_calendar,
    events_from_ff_xml,
    events_from_ics,
    load_event_context,
    parse_calendar_payload,
    refresh_calendar_from_url,
)
from local_console.config import ConsoleConfig

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def write_calendar(
    path: Path,
    events: list[dict[str, object]],
    updated_at: str | None = None,
    *,
    with_trust_marker: bool = True,
) -> None:
    """Write a calendar cache file.

    with_trust_marker=True 模拟 refresh_calendar_from_url 生成的合法缓存
    （带 source/schema_version 标记）；False 模拟手工残留文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "updated_at": updated_at or NOW.isoformat(),
        "events": events,
    }
    if with_trust_marker:
        payload["source"] = "https://test.example/calendar.xml"
        payload["schema_version"] = 1
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def pce_event(hours_from_now: float) -> dict[str, object]:
    instant = NOW + timedelta(hours=hours_from_now)
    return {"title": "美国核心 PCE", "utc": instant.isoformat(), "impact": "high"}


class CalendarEvaluationTests(unittest.TestCase):
    def test_missing_calendar_is_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            result = evaluate_calendar(Path(directory) / "calendar.json", NOW)

        self.assertEqual("unverified", result["status"])
        self.assertIn("缺失", result["reason"])

    def test_stale_calendar_is_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            stale = (NOW - timedelta(hours=72)).isoformat()
            write_calendar(path, [pce_event(5)], updated_at=stale)

            result = evaluate_calendar(path, NOW)

        self.assertEqual("unverified", result["status"])
        self.assertIn("过期", result["reason"])

    def test_high_impact_event_inside_window_is_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            # 事件在 15 分钟后，处于前 60 分钟缓冲窗内
            write_calendar(path, [pce_event(0.25)])

            result = evaluate_calendar(path, NOW)

        self.assertEqual("wait", result["status"])
        self.assertIn("美国核心 PCE", result["reason"])
        self.assertIn("event", result)

    def test_event_outside_window_is_verified_clear_with_next_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            write_calendar(path, [pce_event(3)])

            result = evaluate_calendar(path, NOW)

        self.assertEqual("verified_clear", result["status"])
        self.assertEqual("美国核心 PCE", result["next_event"]["title"])

    def test_low_impact_events_do_not_trigger_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            event = pce_event(0.1)
            event["impact"] = "low"
            write_calendar(path, [event])

            result = evaluate_calendar(path, NOW)

        self.assertEqual("verified_clear", result["status"])

    def test_corrupt_calendar_is_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            path.write_text("not json", encoding="utf-8")

            result = evaluate_calendar(path, NOW)

        self.assertEqual("unverified", result["status"])

    def test_handwritten_calendar_without_source_marker_is_unverified(self):
        """无 source 标记的手工残留文件必须被拒绝，绝不据此 WAIT（事故回归）。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            # 模拟本次事故：手工文件把非农错写成 8/1（周六），且无 source 标记
            bogus = {
                "title": "美国 7 月非农就业报告",
                "utc": "2026-08-01T12:30:00+00:00",
                "impact": "high",
            }
            write_calendar(path, [bogus], with_trust_marker=False)

            result = evaluate_calendar(path, NOW)

        self.assertEqual("unverified", result["status"])
        self.assertNotEqual("wait", result["status"])

    def test_handwritten_calendar_without_schema_version_is_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            payload = {
                "updated_at": NOW.isoformat(),
                "source": "https://test.example/calendar.xml",
                "events": [pce_event(0.25)],  # 在窗口内，若被信任会 WAIT
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = evaluate_calendar(path, NOW)

        self.assertEqual("unverified", result["status"])

    def test_high_impact_event_on_weekend_is_unverified(self):
        """高影响事件落在周末 = 日期数据错误，判 unverified 而非 WAIT（事故回归）。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            # 2026-08-01 是周六：合法缓存文件里出现周末 high 事件同样拒绝
            bogus = {
                "title": "美国 7 月非农就业报告",
                "utc": "2026-08-01T12:30:00+00:00",
                "impact": "high",
            }
            write_calendar(path, [bogus])

            result = evaluate_calendar(path, NOW)

        self.assertEqual("unverified", result["status"])
        self.assertIn("周末", result["reason"])
        self.assertNotEqual("wait", result["status"])

    def test_weekend_event_past_48h_also_unverified(self):
        """周末 high 事件无论过去/未来一律拒绝，防止手工日期错误绕过后窗。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            bogus = {
                "title": "美国 CPI",
                "utc": "2026-08-02T12:30:00+00:00",  # 周日
                "impact": "high",
            }
            write_calendar(path, [bogus])

            result = evaluate_calendar(path, NOW)

        self.assertEqual("unverified", result["status"])

    def test_friday_high_impact_event_still_waits(self):
        """工作日（周五）的高影响事件正常触发 WAIT，合理性校验不误伤。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            # 2026-07-31 是周五，处于前 60 分钟窗口内
            write_calendar(path, [pce_event(0.25)])

            result = evaluate_calendar(path, NOW)

        self.assertEqual("wait", result["status"])
        self.assertIn("美国核心 PCE", result["reason"])

    def test_window_boundaries_cover_before_and_after_buffers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            write_calendar(path, [pce_event(WINDOW_BEFORE_MINUTES / 60)])
            self.assertEqual("wait", evaluate_calendar(path, NOW)["status"])

            write_calendar(path, [pce_event(-WINDOW_AFTER_MINUTES / 60)])
            self.assertEqual("wait", evaluate_calendar(path, NOW)["status"])

            write_calendar(path, [pce_event(WINDOW_BEFORE_MINUTES / 60 + 0.5)])
            self.assertEqual("verified_clear", evaluate_calendar(path, NOW)["status"])

    def test_verified_clear_includes_current_utc(self):
        """verified_clear 结果应包含 current_utc 时间锚点。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            write_calendar(path, [pce_event(5)])

            result = evaluate_calendar(path, NOW)

        self.assertEqual("verified_clear", result["status"])
        self.assertEqual(NOW.isoformat(), result["current_utc"])

    def test_past_events_within_48h_are_reported(self):
        """已过去 48 小时内的高影响事件应出现在 past_events 中。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            # PCE 在 12 小时前公布（已过窗口但仍在 48h 内）
            past_pce = {
                "title": "美国核心 PCE",
                "utc": (NOW - timedelta(hours=12)).isoformat(),
                "impact": "high",
            }
            future_nfp = pce_event(72)  # 72h 后（周一 8/3，工作日）的 NFP
            future_nfp["title"] = "美国非农就业"
            write_calendar(path, [past_pce, future_nfp])

            result = evaluate_calendar(path, NOW)

        self.assertEqual("verified_clear", result["status"])
        self.assertIn("past_events", result)
        self.assertEqual(1, len(result["past_events"]))
        self.assertEqual("美国核心 PCE", result["past_events"][0]["title"])
        self.assertEqual("美国非农就业", result["next_event"]["title"])

    def test_past_events_beyond_48h_are_excluded(self):
        """超过 48 小时的已过去事件不应出现在 past_events 中。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            old_event = {
                "title": "美国 CPI",
                "utc": (NOW - timedelta(hours=60)).isoformat(),
                "impact": "high",
            }
            write_calendar(path, [old_event])

            result = evaluate_calendar(path, NOW)

        self.assertEqual("verified_clear", result["status"])
        self.assertNotIn("past_events", result)


class ManualOverrideTests(unittest.TestCase):
    def make_config(self, root: Path) -> ConsoleConfig:
        return ConsoleConfig(
            repo_root=root,
            state_dir=root / "runtime",
            mt5_python=root / "python.exe",
            mt5_snapshot_script=root / "snapshot.py",
            backend_url=None,
            quick_model="quick",
            deep_model="deep",
        )

    def test_manual_override_wins_over_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            config.state_dir.mkdir(parents=True, exist_ok=True)
            config.event_context_path.write_text(
                json.dumps({"status": "wait", "reason": "人工核验：FOMC 静默期"}),
                encoding="utf-8",
            )
            write_calendar(config.calendar_path, [])

            result = load_event_context(config, NOW)

        self.assertEqual("wait", result["status"])
        self.assertEqual("人工核验：FOMC 静默期", result["reason"])

    def test_invalid_manual_override_falls_back_to_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            config.state_dir.mkdir(parents=True, exist_ok=True)
            config.event_context_path.write_text("garbage", encoding="utf-8")
            write_calendar(config.calendar_path, [])

            result = load_event_context(config, NOW)

        self.assertEqual("verified_clear", result["status"])


FF_XML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<weeklyevents>
  <event>
    <title>Core PCE Price Index m/m</title>
    <country>USD</country>
    <date>07-31-2026</date>
    <time>8:30am</time>
    <impact>High</impact>
    <forecast>0.3%</forecast>
    <previous>0.2%</previous>
  </event>
  <event>
    <title>ECB Press Conference</title>
    <country>EUR</country>
    <date>07-31-2026</date>
    <time>8:45am</time>
    <impact>High</impact>
    <forecast></forecast>
    <previous></previous>
  </event>
  <event>
    <title>FOMC Member Speaks</title>
    <country>USD</country>
    <date>07-31-2026</date>
    <time>Tentative</time>
    <impact>Medium</impact>
    <forecast></forecast>
    <previous></previous>
  </event>
</weeklyevents>
"""

ICS_SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:US Nonfarm Payrolls
DTSTART:20260807T123000Z
END:VEVENT
BEGIN:VEVENT
SUMMARY:US CPI m/m
DTSTART;TZID=America/New_York:20260812T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Weekly Baker Hughes Rig Count
DTSTART:20260807T170000Z
END:VEVENT
END:VCALENDAR
"""


class CalendarSourceParsingTests(unittest.TestCase):
    def test_ff_xml_keeps_usd_and_converts_eastern_to_utc(self):
        events = events_from_ff_xml(FF_XML_SAMPLE)

        # 仅 USD 且时间可解析的条目：Core PCE（EDT 8:30 → UTC 12:30）
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("high", event["impact"])
        self.assertIn("Core PCE", event["title"])
        self.assertEqual("2026-07-31T12:30:00+00:00", event["utc"])

    def test_ics_parses_utc_tzid_and_keyword_impact(self):
        events = events_from_ics(ICS_SAMPLE)
        by_title = {event["title"]: event for event in events}

        self.assertEqual("high", by_title["US Nonfarm Payrolls"]["impact"])
        self.assertEqual("2026-08-07T12:30:00+00:00", by_title["US Nonfarm Payrolls"]["utc"])
        self.assertEqual("high", by_title["US CPI m/m"]["impact"])
        # America/New_York 8:30 EDT → 12:30 UTC
        self.assertEqual("2026-08-12T12:30:00+00:00", by_title["US CPI m/m"]["utc"])
        # 非关键词事件记为 low，不触发 WAIT
        self.assertEqual("low", by_title["Weekly Baker Hughes Rig Count"]["impact"])

    def test_payload_format_detection(self):
        self.assertEqual(1, len(parse_calendar_payload(FF_XML_SAMPLE)))
        self.assertEqual(3, len(parse_calendar_payload(ICS_SAMPLE)))
        self.assertIsNone(parse_calendar_payload("not a calendar"))
        self.assertEqual([], parse_calendar_payload('{"events": []}'))


class CalendarRefreshThrottleTests(unittest.TestCase):
    def setUp(self) -> None:
        # 重置模块级失败退避状态，避免用例间互相污染
        # （test_fetch_failure_keeps_local_file 会设置退避，污染同进程后续用例）。
        import local_console.calendar_refresh as calendar_refresh

        calendar_refresh._last_fetch_failure_at = None

    def make_config(self, root: Path) -> ConsoleConfig:
        return ConsoleConfig(
            repo_root=root,
            state_dir=root / "runtime",
            mt5_python=root / "python.exe",
            mt5_snapshot_script=root / "snapshot.py",
            backend_url=None,
            quick_model="quick",
            deep_model="deep",
        )

    def test_fresh_calendar_file_skips_network(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            write_calendar(config.calendar_path, [])  # mtime=现在，处于 3h 节流窗内

            with patch("local_console.calendar_refresh.urlopen") as opener:
                refreshed = refresh_calendar_from_url(config)

        self.assertFalse(refreshed)
        opener.assert_not_called()

    def test_missing_calendar_fetches_and_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, size: int = -1):
                    return FF_XML_SAMPLE.encode("utf-8")

            with patch("local_console.calendar_refresh.urlopen", return_value=FakeResponse()):
                refreshed = refresh_calendar_from_url(config)
            payload = json.loads(config.calendar_path.read_text(encoding="utf-8"))

        self.assertTrue(refreshed)
        self.assertEqual(1, len(payload["events"]))
        self.assertEqual("high", payload["events"][0]["impact"])

    def test_fetch_failure_keeps_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            write_calendar(config.calendar_path, [])
            stale_time = (NOW - timedelta(hours=10)).timestamp()
            os.utime(config.calendar_path, (stale_time, stale_time))

            with patch("local_console.calendar_refresh.urlopen", side_effect=OSError("down")):
                refreshed = refresh_calendar_from_url(config)

        self.assertFalse(refreshed)
