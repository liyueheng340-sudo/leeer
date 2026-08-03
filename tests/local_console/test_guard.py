from __future__ import annotations

import unittest
from datetime import UTC, datetime

from local_console.guard import SPREAD_DOWNGRADE_THRESHOLD, evaluate_gate


def has_warning(warnings: tuple[str, ...], text: str) -> bool:
    """warnings 是完整句子元组：断言需做子串匹配，而非成员匹配。"""
    return any(text in w for w in warnings)


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
    """军师模式（宪法）：数据不可用才 BLOCKED，其余风险一律转为 warnings 标注。"""

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

    def test_unverified_events_stay_analyse_with_warning(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "unverified"},
            NOW,
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(result.directional_plan_allowed)
        self.assertTrue(has_warning(result.warnings, "事件上下文未核验"))

    def test_event_wait_stays_analyse_with_warning(self):
        # 宪法：事件窗口只标注不锁死——分析永远可用，风险随报告呈现。
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "wait", "reason": "高影响事件窗口"},
            NOW,
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(result.directional_plan_allowed)
        self.assertTrue(has_warning(result.warnings, "高影响事件窗口"))

    def test_verified_clear_with_healthy_tick_is_analyse_without_warnings(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            healthy_tick(),
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(result.directional_plan_allowed)
        self.assertEqual((), result.warnings)

    def test_verified_clear_without_tick_sensor_stays_analyse(self):
        # 传感器不可用/缺失时不做标注判断——tick 流只是辅助读数
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            {"available": False, "reason": "MT5 Python 解释器不可用"},
        )

        self.assertEqual("ANALYSE", result.action)

    def test_stalled_tick_flow_stays_analyse_with_warning(self):
        tick = healthy_tick()
        tick["stalled"] = True
        tick["stall_reason"] = "最近 tick 距今 42 秒"

        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            tick,
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(result.directional_plan_allowed)
        self.assertTrue(has_warning(result.warnings, "停滞"))

    def test_spread_spike_stays_analyse_with_warning(self):
        # 2026-08-03 升级：点差异常 → 禁方向（directional_plan_allowed=False），
        # 分析保留（allow_model=True）——军师模式不锁死分析，但入场成本高时不建议方向。
        tick = healthy_tick()
        tick["spread_max"] = SPREAD_DOWNGRADE_THRESHOLD + 0.4

        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            tick,
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertFalse(result.directional_plan_allowed)
        self.assertTrue(has_warning(result.warnings, "点差异常扩大"))

    def test_spread_high_percentile_blocks_direction(self):
        # 2026-08-03 升级：点差处于近期历史高位（≥80 分位）→ 禁方向。
        # 依据：实盘 33 单点差≥80分位胜率 12%（-23.3R）vs 正常组 46%（+9.9R）。
        tick = healthy_tick()
        tick["spread_percentile"] = 0.9

        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            tick,
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertFalse(result.directional_plan_allowed)
        self.assertTrue(has_warning(result.warnings, "禁方向建议"))

    def test_warnings_accumulate_with_event_wait_and_tick_stall(self):
        # 多条风险同时存在 → 全部进入 warnings，仍不阻断分析
        tick = healthy_tick()
        tick["stalled"] = True

        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "wait", "reason": "高影响事件窗口"},
            NOW,
            tick,
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(has_warning(result.warnings, "高影响事件窗口"))
        self.assertTrue(has_warning(result.warnings, "停滞"))
        self.assertEqual(2, len(result.warnings))


class EdgeDowngradeTests(unittest.TestCase):
    """1.3.0→1.5.0：共振不明确 / 非活跃时段由降级改为风险标注。"""

    def snapshot_with_structure(self, structure: dict[str, object]) -> dict[str, object]:
        snapshot = fresh_snapshot()
        snapshot["timeframe_structure"] = structure
        return snapshot

    def test_unclear_resonance_stays_analyse_with_warning(self):
        # m5 多一票 vs h4 空一票 → score=0.6? 权重 (4,-1)/10=0.3 → 不明确
        # m15 空(-2) + h4 多(+4) → score=(4-2)/(4+2)=+0.33 → 不明确
        snapshot = self.snapshot_with_structure({
            "m15": {"body_direction": "sell", "change_4": -1.0},
            "h4": {"body_direction": "buy", "change_4": 1.0},
        })

        result = evaluate_gate(
            snapshot, {"status": "verified_clear"}, NOW, healthy_tick()
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(result.directional_plan_allowed)
        self.assertTrue(has_warning(result.warnings, "共振不明确"))

    def test_clear_resonance_keeps_analyse_without_warning(self):
        snapshot = self.snapshot_with_structure({
            "m5": {"body_direction": "buy", "change_4": 1.0},
            "m15": {"body_direction": "buy", "change_4": 1.0},
            "h1": {"body_direction": "buy", "change_4": 1.0},
            "h4": {"body_direction": "buy", "change_4": 1.0},
        })

        result = evaluate_gate(
            snapshot, {"status": "verified_clear"}, NOW, healthy_tick()
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertNotIn("共振不明确", result.warnings)

    def test_inactive_session_stays_analyse_with_warning(self):
        # 2026-08-03 升级：scalp 模式亚洲时段 → 禁方向（剥头皮禁区）；
        # swing 模式保留方向（波段可持仓过渡时段）。
        snapshot = fresh_snapshot()
        snapshot["session_label"] = "asia"

        result = evaluate_gate(
            snapshot, {"status": "verified_clear"}, NOW, healthy_tick(), mode="scalp"
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertFalse(result.directional_plan_allowed)
        self.assertTrue(has_warning(result.warnings, "禁方向建议"))

    def test_swing_mode_keeps_direction_in_asia(self):
        # swing（日内波段）不受亚洲时段禁方向限制：可持仓过渡非活跃时段。
        snapshot = fresh_snapshot()
        snapshot["session_label"] = "asia"

        result = evaluate_gate(
            snapshot, {"status": "verified_clear"}, NOW, healthy_tick(), mode="swing"
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.directional_plan_allowed)
        self.assertTrue(has_warning(result.warnings, "流动性不足"))  # 仍标注但不禁方向

    def test_active_session_keeps_analyse_without_warning(self):
        snapshot = fresh_snapshot()
        snapshot["session_label"] = "london_ny_overlap"

        result = evaluate_gate(
            snapshot, {"status": "verified_clear"}, NOW, healthy_tick()
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertEqual((), result.warnings)

    def test_missing_session_label_does_not_warn(self):
        result = evaluate_gate(
            fresh_snapshot(), {"status": "verified_clear"}, NOW, healthy_tick()
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertEqual((), result.warnings)


class RegimeDowngradeTests(unittest.TestCase):
    """1.4.0→1.5.0：震荡市（双周期 ADX<20）由降级改为风险标注（EA 精华参数工程）。"""

    def test_ranging_regime_stays_analyse_with_warning(self):
        # 共振偏多（方向一致）但双周期 ADX 均 <20 → 震荡市标注
        snapshot = fresh_snapshot()
        snapshot["timeframe_structure"] = {
            "m15": {"body_direction": "buy", "change_4": 1.0, "adx_14": 15.0},
            "h1": {"body_direction": "buy", "change_4": 1.0, "adx_14": 12.0},
        }

        result = evaluate_gate(
            snapshot, {"status": "verified_clear"}, NOW, healthy_tick()
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(result.directional_plan_allowed)
        self.assertTrue(has_warning(result.warnings, "震荡市"))

    def test_trending_regime_keeps_analyse_without_warning(self):
        snapshot = fresh_snapshot()
        snapshot["timeframe_structure"] = {
            "m15": {"body_direction": "buy", "change_4": 1.0, "adx_14": 30.0},
            "h1": {"body_direction": "buy", "change_4": 1.0, "adx_14": 28.0},
        }

        result = evaluate_gate(
            snapshot, {"status": "verified_clear"}, NOW, healthy_tick()
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertNotIn("震荡市", result.warnings)

    def test_missing_regime_data_does_not_warn(self):
        # 无 ADX 指标（旧快照/字段缺失）→ 状态不可用，不标注
        snapshot = fresh_snapshot()
        snapshot["timeframe_structure"] = {
            "m15": {"body_direction": "buy", "change_4": 1.0},
            "h1": {"body_direction": "buy", "change_4": 1.0},
        }

        result = evaluate_gate(
            snapshot, {"status": "verified_clear"}, NOW, healthy_tick()
        )

        self.assertEqual("ANALYSE", result.action)
        self.assertEqual((), result.warnings)
