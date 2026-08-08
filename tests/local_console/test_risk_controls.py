"""Tests for dynamic risk controls (方案 B 连亏熔断)。

2026-08-07：连亏熔断（连续 N 单 SL_FIRST 后禁方向，保留分析）。
否决权来自测量（复盘 outcome），不来自意见；只禁方向，不新增 BLOCKED。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from local_console.guard import GateResult
from local_console.risk_controls import (
    LOSS_STREAK_THRESHOLD,
    apply_circuit_breaker,
    circuit_breaker_reason,
    circuit_tripped,
    compute_loss_streak,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def make_record(outcome: str, created_at: str = "2026-08-01T00:00:00+00:00") -> object:
    class _R:
        pass

    _R.review = {"outcome": outcome}
    _R.created_at = created_at
    return _R()


def gate(allowed: bool = True) -> GateResult:
    return GateResult("ANALYSE", True, allowed, "ok", warnings=())


class LossStreakTests(unittest.TestCase):
    def test_no_review_is_zero_streak(self):
        self.assertEqual(0, compute_loss_streak([]))
        self.assertEqual(0, compute_loss_streak([make_record("PENDING")]))

    def test_single_loss_is_streak_one(self):
        records = [make_record("SL_FIRST")]
        self.assertEqual(1, compute_loss_streak(records))

    def test_three_consecutive_losses_is_streak_three(self):
        records = [make_record("SL_FIRST") for _ in range(3)]
        self.assertEqual(3, compute_loss_streak(records))

    def test_loss_then_win_resets_streak(self):
        # 最新是 TP → 连亏计数归零
        records = [make_record("SL_FIRST"), make_record("SL_FIRST"), make_record("TP_FIRST")]
        self.assertEqual(0, compute_loss_streak(records))

    def test_win_then_loss_counts_from_latest(self):
        # 从最新往回数：最后两个是 SL → streak 2
        records = [make_record("TP_FIRST"), make_record("SL_FIRST"), make_record("SL_FIRST")]
        self.assertEqual(2, compute_loss_streak(records))

    def test_non_decided_outcomes_do_not_reset_streak(self):
        # NOT_TRIGGERED 不参与：不重置也不累加连亏
        records = [make_record("SL_FIRST"), make_record("SL_FIRST"), make_record("NOT_TRIGGERED")]
        self.assertEqual(2, compute_loss_streak(records))


class CircuitBreakerTests(unittest.TestCase):
    def test_below_threshold_returns_none(self):
        self.assertIsNone(circuit_breaker_reason(2, LOSS_STREAK_THRESHOLD))

    def test_at_threshold_returns_reason(self):
        reason = circuit_breaker_reason(LOSS_STREAK_THRESHOLD, LOSS_STREAK_THRESHOLD)
        self.assertIsNotNone(reason)
        self.assertIn("连亏熔断", reason)

    def test_apply_trips_direction_when_at_threshold(self):
        g = gate(allowed=True)
        result = apply_circuit_breaker(g, LOSS_STREAK_THRESHOLD)
        self.assertFalse(result.directional_plan_allowed)
        self.assertTrue(result.allow_model)  # 分析保留
        self.assertIn("连亏熔断", result.reason)
        self.assertTrue(any("连亏熔断" in w for w in result.warnings))

    def test_apply_below_threshold_unchanged(self):
        g = gate(allowed=True)
        result = apply_circuit_breaker(g, 1)
        self.assertTrue(result.directional_plan_allowed)
        self.assertEqual((), result.warnings)

    def test_apply_preserves_existing_warnings(self):
        g = GateResult("ANALYSE", True, True, "ok", warnings=("点差高位",))
        result = apply_circuit_breaker(g, LOSS_STREAK_THRESHOLD)
        self.assertIn("点差高位", result.warnings)
        self.assertTrue(any("连亏熔断" in w for w in result.warnings))


class CircuitTrippedStateMachineTests(unittest.TestCase):
    """circuit_tripped：4 连亏触发 + 2 非亏损恢复的完整状态机。"""

    def test_below_threshold_not_tripped(self):
        records = [make_record("SL_FIRST") for _ in range(3)]
        self.assertIsNone(circuit_tripped(records, LOSS_STREAK_THRESHOLD))

    def test_at_threshold_tripped(self):
        records = [make_record("SL_FIRST") for _ in range(LOSS_STREAK_THRESHOLD)]
        result = circuit_tripped(records, LOSS_STREAK_THRESHOLD)
        self.assertEqual(LOSS_STREAK_THRESHOLD, result)

    def test_win_breaks_streak_before_trip(self):
        # 未触发前，TP 中断连亏计数
        records = [make_record("SL_FIRST") for _ in range(3)]
        records.append(make_record("TP_FIRST"))
        self.assertIsNone(circuit_tripped(records, LOSS_STREAK_THRESHOLD))

    def test_tripped_then_single_non_loss_still_tripped(self):
        # 触发后，1 单 TP 不足以恢复（需 2 单非亏损）
        records = [make_record("SL_FIRST") for _ in range(4)] + [make_record("TP_FIRST")]
        self.assertEqual(4, circuit_tripped(records, LOSS_STREAK_THRESHOLD))

    def test_tripped_then_two_non_loss_recovers(self):
        # 触发后，2 单非亏损恢复
        records = [make_record("SL_FIRST") for _ in range(4)] + [
            make_record("TP_FIRST"), make_record("TP_FIRST"),
        ]
        self.assertIsNone(circuit_tripped(records, LOSS_STREAK_THRESHOLD))

    def test_other_outcome_counts_as_recovery(self):
        # NOT_TRIGGERED 也计入非亏损恢复信号
        records = [make_record("SL_FIRST") for _ in range(4)] + [
            make_record("NOT_TRIGGERED"), make_record("TP_FIRST"),
        ]
        self.assertIsNone(circuit_tripped(records, LOSS_STREAK_THRESHOLD))

    def test_recovery_requires_two_not_one(self):
        # 1 单非亏损仍熔断（验证 RECOVER_NON_LOSS=2 生效）
        records = [make_record("SL_FIRST") for _ in range(4)] + [make_record("TP_FIRST")]
        self.assertEqual(4, circuit_tripped(records, LOSS_STREAK_THRESHOLD, recover=2))


if __name__ == "__main__":
    unittest.main()
