from __future__ import annotations

import unittest
from datetime import datetime, timezone

from local_console.session_context import (
    SESSION_CONTEXT_KEY,
    SESSION_LABEL_KEY,
    _minutes_to_next,
    _session_at,
    compute_session_context,
)

# 2026-07-15 是夏令时（BST=UTC+1, EDT=UTC-4）。
# 伦敦 10:00 BST = 09:00 UTC。
SUMMER_NOW = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
# 2026-01-15 是冬令时（GMT=UTC+0, EST=UTC-5）。
# 伦敦 10:00 GMT = 10:00 UTC。
WINTER_NOW = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)


class SessionAtTest(unittest.TestCase):
    def test_london_morning_is_active(self):
        # 09:00 UTC = 10:00 BST（夏时制）→ 伦敦早盘（07:00-13:30 London）
        label, name = _session_at(SUMMER_NOW)
        self.assertEqual(label, "london")
        self.assertTrue(name)

    def test_overlap_window(self):
        # 13:00 UTC = 14:00 BST → 重叠时段（13:30-16:30 London）
        now = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
        label, _ = _session_at(now)
        self.assertEqual(label, "london_ny_overlap")

    def test_ny_late_window(self):
        # 17:00 UTC = 18:00 BST → 纽约尾盘（16:30-21:00 London）
        now = datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc)
        label, _ = _session_at(now)
        self.assertEqual(label, "ny_late")

    def test_asia_is_inactive(self):
        # 03:00 UTC = 04:00 BST → 隔夜/亚洲时段
        now = datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc)
        label, _ = _session_at(now)
        self.assertEqual(label, "asia")

    def test_late_night_returns_asia(self):
        # 22:00 UTC = 23:00 BST → 落入 asia（21:00 之后）
        now = datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc)
        label, _ = _session_at(now)
        self.assertEqual(label, "asia")


class MinutesToNextTest(unittest.TestCase):
    def test_next_fix_within_same_day(self):
        from zoneinfo import ZoneInfo

        london = ZoneInfo("Europe/London")
        now = datetime(2026, 7, 15, 10, 0, tzinfo=london)  # 10:00 BST
        minutes = _minutes_to_next(now, london, 10, 30)
        self.assertEqual(minutes, 30)

    def test_next_fix_crosses_day(self):
        from zoneinfo import ZoneInfo

        london = ZoneInfo("Europe/London")
        now = datetime(2026, 7, 15, 16, 0, tzinfo=london)  # 已过 15:00 定盘
        minutes = _minutes_to_next(now, london, 10, 30)
        self.assertEqual(minutes, 18 * 60 + 30)  # 到次日 10:30

    def test_exact_fix_is_zero(self):
        from zoneinfo import ZoneInfo

        london = ZoneInfo("Europe/London")
        now = datetime(2026, 7, 15, 15, 0, tzinfo=london)
        minutes = _minutes_to_next(now, london, 15, 0)
        self.assertEqual(minutes, 24 * 60)  # 恰好时刻 → 到次日同一时刻


class ComputeSessionContextTest(unittest.TestCase):
    def test_summer_success_shape(self):
        result = compute_session_context(SUMMER_NOW)
        self.assertEqual(result["status"], "ok")
        self.assertIn(result["label"], {"asia", "london", "london_ny_overlap", "ny_late"})
        self.assertIsInstance(result["minutes_to_london_fix"], int)
        self.assertIsInstance(result["minutes_to_comex_open"], int)
        self.assertTrue(result["london_fix_at"].endswith("+01:00"))  # BST
        self.assertTrue(result["comex_open_at"].endswith("-04:00"))  # EDT

    def test_winter_dst_offset(self):
        result = compute_session_context(WINTER_NOW)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["london_fix_at"].endswith("+00:00"))  # GMT
        self.assertTrue(result["comex_open_at"].endswith("-05:00"))  # EST

    def test_london_fix_is_nearest_occurrence(self):
        # 伦敦 11:00 BST：最近定盘是 15:00（240 分钟后），而非次日 10:30。
        now = datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc)  # 12:00 BST
        result = compute_session_context(now)
        self.assertEqual(result["minutes_to_london_fix"], 180)  # 15:00 - 12:00

    def test_label_matches_guard_semantics(self):
        """活跃时段 label 必须与 guard.ACTIVE_SESSION_LABELS 语义一致。"""
        from local_console.guard import ACTIVE_SESSION_LABELS

        result = compute_session_context(SUMMER_NOW)
        label = result["label"]
        if label == "asia":
            self.assertNotIn(label, ACTIVE_SESSION_LABELS)
        else:
            self.assertIn(label, ACTIVE_SESSION_LABELS)


class InjectionContractTest(unittest.TestCase):
    def test_snapshot_keys_exported(self):
        """job_runner 注入用到的键必须在本模块导出。"""
        self.assertEqual(SESSION_LABEL_KEY, "session_label")
        self.assertEqual(SESSION_CONTEXT_KEY, "session_context")


if __name__ == "__main__":
    unittest.main()
