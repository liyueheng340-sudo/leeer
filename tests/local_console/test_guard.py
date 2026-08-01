from __future__ import annotations

import unittest
from datetime import UTC, datetime

from local_console.guard import SPREAD_DOWNGRADE_THRESHOLD, evaluate_gate


def fresh_snapshot() -> dict[str, object]:
    return {
        "timestamp": "2026-07-30T00:01:30+00:00",
        "identity_match": True,
        "symbol": "XAUUSD",
        "bid": 4000.0,
        "ask": 4000.1,
    }


def healthy_tick() -> dict[str, object]:
    return {
        "available": True,
        "ticks": 420,
        "spread_median": 0.1,
        "spread_max": 0.2,
        "stalled": False,
    }


NOW = datetime(2026, 7, 30, 0, 1, 45, tzinfo=UTC)


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

    def test_unverified_events_force_watch_with_technical_only_plan(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "unverified"},
            NOW,
        )

        self.assertEqual("WATCH", result.action)
        self.assertTrue(result.allow_model)
        # WATCH 门态允许技术面方向建议（事件未核验，须带警示）
        self.assertTrue(result.directional_plan_allowed)
        self.assertIn("事件上下文未核验", result.reason)

    def test_event_wait_blocks_model_request(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "wait", "reason": "高影响事件窗口"},
            NOW,
        )

        self.assertEqual("WAIT", result.action)
        self.assertEqual("高影响事件窗口", result.reason)
        self.assertFalse(result.allow_model)

    def test_verified_clear_with_healthy_tick_is_analyse(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            healthy_tick(),
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(result.directional_plan_allowed)

    def test_verified_clear_without_tick_sensor_stays_analyse(self):
        # 传感器不可用/缺失时不做降级判断——tick 流只是辅助触发器
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            {"available": False, "reason": "MT5 Python 解释器不可用"},
        )

        self.assertEqual("ANALYSE", result.action)

    def test_stalled_tick_flow_downgrades_analyse_to_watch(self):
        tick = healthy_tick()
        tick["stalled"] = True
        tick["stall_reason"] = "最近 tick 距今 42 秒"

        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            tick,
        )

        self.assertEqual("WATCH", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(result.directional_plan_allowed)
        self.assertIn("停滞", result.reason)

    def test_spread_spike_downgrades_analyse_to_watch(self):
        tick = healthy_tick()
        tick["spread_max"] = SPREAD_DOWNGRADE_THRESHOLD + 0.4

        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            tick,
        )

        self.assertEqual("WATCH", result.action)
        self.assertIn("点差异常扩大", result.reason)

    def test_tick_downgrade_does_not_override_event_wait(self):
        tick = healthy_tick()
        tick["stalled"] = True

        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "wait", "reason": "高影响事件窗口"},
            NOW,
            tick,
        )

        self.assertEqual("WAIT", result.action)
        self.assertFalse(result.allow_model)
