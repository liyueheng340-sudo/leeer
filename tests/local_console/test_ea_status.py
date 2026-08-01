from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from local_console.config import ConsoleConfig
from local_console.ea_status import read_ea_status
from local_console.guard import ea_downgrade_reason, evaluate_gate
from local_console.service import ConsoleService

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


def write_status(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def fresh_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ea": "Cerberus",
        "version": "1.17.1",
        "gmt": "2026.07.31 11:59:30",  # NOW 前 30 秒，新鲜
        "status": "RUNNING",
        "hour": {"risk": "LOW", "blocked": False, "change_min": 45, "sched_blocked": False},
        "regime_blocked": False,
        "feed": "OK",
    }
    payload.update(overrides)
    return payload


class ReadEaStatusTests(unittest.TestCase):
    def test_none_path_is_unavailable(self):
        result = read_ea_status(None, NOW)
        self.assertIs(False, result["available"])

    def test_missing_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            result = read_ea_status(Path(directory) / "ng_status.json", NOW)
        self.assertIs(False, result["available"])

    def test_invalid_json_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ng_status.json"
            path.write_text("{not json", encoding="utf-8")
            result = read_ea_status(path, NOW)
        self.assertIs(False, result["available"])

    def test_non_dict_payload_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ng_status.json"
            write_status(path, ["RUNNING"])
            result = read_ea_status(path, NOW)
        self.assertIs(False, result["available"])

    def test_missing_gmt_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ng_status.json"
            payload = fresh_payload()
            del payload["gmt"]
            write_status(path, payload)
            result = read_ea_status(path, NOW)
        self.assertIs(False, result["available"])

    def test_bad_gmt_format_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ng_status.json"
            write_status(path, fresh_payload(gmt="2026-07-31T11:59:30Z"))
            result = read_ea_status(path, NOW)
        self.assertIs(False, result["available"])

    def test_stale_status_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ng_status.json"
            # 300 秒前写入，远超 120 秒阈值（EA 停写场景）
            write_status(path, fresh_payload(gmt="2026.07.31 11:55:00"))
            result = read_ea_status(path, NOW, max_age_seconds=120)
        self.assertIs(False, result["available"])
        self.assertIn("陈旧", str(result["reason"]))

    def test_fresh_status_is_available_with_risk_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ng_status.json"
            write_status(
                path,
                fresh_payload(
                    status="PAUSED_VOLATILITY",
                    regime_blocked=True,
                    hour={"risk": "VERY HIGH", "blocked": True},
                ),
            )
            result = read_ea_status(path, NOW)
        self.assertIs(True, result["available"])
        self.assertEqual("PAUSED_VOLATILITY", result["status"])
        self.assertIs(True, result["regime_blocked"])
        self.assertIs(True, result["hour_blocked"])
        self.assertEqual("OK", result["feed"])
        self.assertAlmostEqual(30.0, result["age_seconds"], delta=1.0)

    def test_missing_hour_block_defaults_to_false(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ng_status.json"
            payload = fresh_payload()
            del payload["hour"]
            write_status(path, payload)
            result = read_ea_status(path, NOW)
        self.assertIs(True, result["available"])
        self.assertIs(False, result["hour_blocked"])


class EaGateMappingTests(unittest.TestCase):
    def test_unavailable_ea_status_does_not_change_gate(self):
        self.assertIsNone(ea_downgrade_reason({"available": False, "reason": "陈旧"}))
        self.assertIsNone(ea_downgrade_reason(None))

    def test_paused_news_maps_to_wait(self):
        result = ea_downgrade_reason({"available": True, "status": "PAUSED_NEWS"})
        self.assertIsNotNone(result)
        level, reason = result
        self.assertEqual("WAIT", level)
        self.assertIn("新闻事件窗口", reason)

    def test_paused_volatility_maps_to_watch(self):
        result = ea_downgrade_reason({"available": True, "status": "PAUSED_VOLATILITY"})
        self.assertEqual("WATCH", result[0])
        self.assertIn("波动率熔断", result[1])

    def test_regime_blocked_maps_to_watch(self):
        result = ea_downgrade_reason({"available": True, "status": "RUNNING", "regime_blocked": True})
        self.assertEqual("WATCH", result[0])
        self.assertIn("强趋势", result[1])

    def test_hour_blocked_maps_to_watch(self):
        result = ea_downgrade_reason({"available": True, "status": "RUNNING", "hour_blocked": True})
        self.assertEqual("WATCH", result[0])
        self.assertIn("高危波动窗口", result[1])

    def test_combined_watch_reasons_are_joined(self):
        result = ea_downgrade_reason(
            {
                "available": True,
                "status": "PAUSED_VOLATILITY",
                "regime_blocked": True,
                "hour_blocked": True,
            }
        )
        self.assertEqual("WATCH", result[0])
        self.assertIn("波动率熔断", result[1])
        self.assertIn("强趋势", result[1])
        self.assertIn("高危波动窗口", result[1])

    def test_running_status_means_no_downgrade(self):
        self.assertIsNone(ea_downgrade_reason({"available": True, "status": "RUNNING"}))

    def test_manual_and_schedule_pauses_are_not_market_evidence(self):
        # 人工暂停/计划暂停是操作选择，不反映市场风险，不触发降级
        self.assertIsNone(ea_downgrade_reason({"available": True, "status": "PAUSED_MANUAL"}))
        self.assertIsNone(ea_downgrade_reason({"available": True, "status": "PAUSED_SCHEDULE"}))


def fresh_snapshot() -> dict[str, object]:
    return {
        "timestamp": "2026-07-31T11:59:30+00:00",
        "identity_match": True,
        "symbol": "XAUUSD",
        "bid": 4000.0,
        "ask": 4000.1,
    }


class EvaluateGateWithEaTests(unittest.TestCase):
    def test_ea_news_window_forces_wait_when_calendar_unverified(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "unverified"},
            NOW,
            None,
            {"available": True, "status": "PAUSED_NEWS"},
        )
        self.assertEqual("WAIT", result.action)
        self.assertFalse(result.allow_model)

    def test_event_wait_keeps_priority_over_ea_reason(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "wait", "reason": "高影响事件窗口"},
            NOW,
            None,
            {"available": True, "status": "PAUSED_NEWS"},
        )
        self.assertEqual("WAIT", result.action)
        self.assertEqual("高影响事件窗口", result.reason)

    def test_ea_volatility_downgrades_analyse_to_watch(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            None,
            {"available": True, "status": "PAUSED_VOLATILITY"},
        )
        self.assertEqual("WATCH", result.action)
        self.assertTrue(result.allow_model)
        self.assertTrue(result.directional_plan_allowed)
        self.assertIn("波动率熔断", result.reason)

    def test_unavailable_ea_status_keeps_analyse(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
            None,
            {"available": False, "reason": "EA 状态已陈旧"},
        )
        self.assertEqual("ANALYSE", result.action)

    def test_no_ea_status_argument_preserves_previous_behavior(self):
        result = evaluate_gate(
            fresh_snapshot(),
            {"status": "verified_clear"},
            NOW,
        )
        self.assertEqual("ANALYSE", result.action)


def make_config(root: Path) -> ConsoleConfig:
    return ConsoleConfig(
        repo_root=root,
        state_dir=root / "runtime",
        mt5_python=root / "python.exe",
        mt5_snapshot_script=root / "snapshot.py",
        backend_url="https://example.invalid/v1",
        quick_model="quick",
        deep_model="deep",
    )


def fake_snapshot(_config: ConsoleConfig, _job_id: str) -> dict[str, object]:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "identity_match": True,
        "symbol": "XAUUSD",
        "bid": 4000.0,
        "ask": 4000.1,
    }


def wait_terminal(service: ConsoleService, job_id: str) -> object:
    deadline = time.monotonic() + 2
    current = service.get(job_id)
    while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
        time.sleep(0.01)
        current = service.get(job_id)
    return current


class ServiceEaWiringTests(unittest.TestCase):
    def make_service(self, config: ConsoleConfig, **overrides: object) -> ConsoleService:
        kwargs = {
            "tick_runner": lambda _c, _j: {"available": False, "reason": "测试环境无 MT5"},
            "macro_runner": lambda _c: {"status": "unavailable", "reason": "测试环境无 FRED"},
            "news_runner": lambda _c: {"status": "unavailable", "reason": "测试环境无网络"},
            **overrides,
        }
        return ConsoleService(config, **kwargs)

    def test_ea_news_window_completes_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            model_called = []

            def spy_brief(_c, _k, _facts, _gate) -> object:
                model_called.append(True)
                return {}

            service = self.make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=spy_brief,
                ea_status_runner=lambda _c: {
                    "available": True,
                    "status": "PAUSED_NEWS",
                    "regime_blocked": False,
                    "hour_blocked": False,
                    "feed": "OK",
                    "age_seconds": 12.0,
                },
            )
            try:
                created = service.start("brief")
                current = wait_terminal(service, created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        self.assertFalse(model_called, "EA 新闻窗口下不得调用模型")
        self.assertEqual("WAIT", current.gate["action"])
        self.assertIn("新闻事件窗口", current.gate["reason"])
        self.assertEqual("PAUSED_NEWS", current.gate["ea_status"]["status"])
        # 纪律：gate_payload 只带风险机制字段，不含持仓/盈亏
        self.assertNotIn("positions", current.gate["ea_status"])
        self.assertNotIn("realized_pl", current.gate["ea_status"])

    def test_unavailable_ea_status_keeps_analyse_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))

            def fake_brief(_c, _k, _facts, _gate) -> object:
                return {
                    "action": "ANALYSE",
                    "source_ids": ["mt5_snapshot", "verified_event_context"],
                    "summary": "快照可用于人工分析。",
                    "invalidation": "后续快照会替代本次观察。",
                    "next_observation": "下一根 M1 收盘后刷新。",
                    "evidence_fields": ["bid", "symbol"],
                    "direction": "LONG",
                    "entry_zone": "3995-4005",
                    "take_profit": "4015",
                    "stop_loss": "3985",
                    "risk_note": "测试环境风险提示。",
                }

            service = self.make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
                ea_status_runner=lambda _c: {"available": False, "reason": "EA 状态文件不存在"},
            )
            try:
                created = service.start("brief")
                current = wait_terminal(service, created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        self.assertEqual("ANALYSE", current.gate["action"])
        self.assertIsNone(current.gate["ea_status"])


if __name__ == "__main__":
    unittest.main()
