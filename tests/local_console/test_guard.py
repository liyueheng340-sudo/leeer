from __future__ import annotations

import unittest
from datetime import UTC, datetime

from local_console.guard import evaluate_gate


def fresh_snapshot() -> dict[str, object]:
    return {
        "timestamp": "2026-07-30T00:01:30+00:00",
        "identity_match": True,
        "symbol": "XAUUSD",
        "bid": 4000.0,
        "ask": 4000.1,
    }


class GateTests(unittest.TestCase):
    def test_stale_snapshot_is_blocked(self):
        snapshot = fresh_snapshot()
        snapshot["timestamp"] = "2026-07-30T00:00:00+00:00"

        result = evaluate_gate(
            snapshot,
            {"status": "verified_clear"},
            datetime(2026, 7, 30, 0, 2, tzinfo=UTC),
        )

        self.assertEqual("BLOCKED", result.action)
        self.assertEqual("MT5 快照已超过 60 秒", result.reason)
        self.assertFalse(result.allow_model)

    def test_unverified_events_force_watch_with_no_directional_plan(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "unverified"},
            datetime(2026, 7, 30, 0, 1, 45, tzinfo=UTC),
        )

        self.assertEqual("WATCH", result.action)
        self.assertTrue(result.allow_model)
        self.assertFalse(result.directional_plan_allowed)

    def test_event_wait_blocks_model_request(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "wait", "reason": "高影响事件窗口"},
            datetime(2026, 7, 30, 0, 1, 45, tzinfo=UTC),
        )

        self.assertEqual("WAIT", result.action)
        self.assertEqual("高影响事件窗口", result.reason)
        self.assertFalse(result.allow_model)
