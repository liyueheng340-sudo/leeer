from __future__ import annotations

import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import patch

from local_console.brief import PROMPT_VERSION
from local_console.config import ConsoleConfig
from local_console.jobs import JobStore
from local_console.service import ConsoleService


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


def fake_tick_unavailable(_config: ConsoleConfig, _job_id: str) -> dict[str, object]:
    return {"available": False, "reason": "测试环境无 MT5"}


def fake_macro_unavailable(_config: ConsoleConfig) -> dict[str, object]:
    return {"status": "unavailable", "reason": "测试环境无 FRED"}


def fake_news_unavailable(_config: ConsoleConfig) -> dict[str, object]:
    return {"status": "unavailable", "reason": "测试环境无网络"}


def fake_news_ok(_config: ConsoleConfig) -> dict[str, object]:
    return {
        "status": "ok",
        "as_of": "2026-07-31T12:00:00+00:00",
        "frequency": "recent",
        "note": "近期新闻背景",
        "items": [
            {"title": "Gold surges on Fed cut", "publisher": "Reuters", "utc": "2026-07-31T10:00:00+00:00", "summary": "", "link": ""},
            {"title": "CPI data beats", "publisher": "Bloomberg", "utc": "2026-07-31T09:00:00+00:00", "summary": "", "link": ""},
        ],
        "fetched_at": "2026-07-31T12:00:00+00:00",
    }


def fake_brief(_config: ConsoleConfig, _kind: str, _snapshot: dict[str, object], _gate: object, _mode: str = "scalp") -> object:
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
        "suggestions": ["若回踩 3995-4005 不破可入场", "突破后关注量能确认"],
        "scenarios": ["若跌破 3985 立即离场", "若冲高遇阻减仓一半"],
        "avoid": ["不追突破", "不在数据窗口前重仓"],
    }


def fake_iv_unavailable(_config: ConsoleConfig) -> dict[str, object]:
    return {"status": "unavailable", "reason": "测试环境无 IV 数据源"}


def make_service(config: ConsoleConfig, **overrides) -> ConsoleService:
    kwargs = {
        "tick_runner": fake_tick_unavailable,
        "macro_runner": fake_macro_unavailable,
        "news_runner": fake_news_unavailable,
        "iv_runner": fake_iv_unavailable,
        **overrides,
    }
    return ConsoleService(config, **kwargs)


class ConsoleServiceTests(unittest.TestCase):
    def test_start_reuses_the_active_job_instead_of_queueing_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            entered_model = Event()
            release_model = Event()

            def slow_brief(
                brief_config: ConsoleConfig,
                kind: str,
                snapshot: dict[str, object],
                gate: object,
                _mode: str = "scalp",
            ) -> object:
                entered_model.set()
                release_model.wait(1)
                return fake_brief(brief_config, kind, snapshot, gate, _mode)

            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=slow_brief,
            )
            try:
                first = service.start("brief")
                self.assertTrue(entered_model.wait(1))
                second = service.start("deep_review")
            finally:
                release_model.set()
                service.close()

        self.assertEqual(first.id, second.id)

    def test_service_recovers_stale_job_on_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            stale_store = JobStore(config.jobs_dir)
            job = stale_store.create("brief")
            record = stale_store.get(job.id)
            record.stage = "MODEL"
            record.updated_at = "2000-01-01T00:00:00+00:00"
            stale_store._write(record)

            service = make_service(config)
            try:
                recovered = service.get(job.id)
            finally:
                service.close()

        self.assertEqual("FAILED", recovered.stage)

    def test_polling_recovers_a_job_that_becomes_stale_after_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(config)
            try:
                job = service.store.create("brief")
                record = service.store.get(job.id)
                record.stage = "MODEL"
                record.updated_at = "2000-01-01T00:00:00+00:00"
                service.store._write(record)

                recovered = service.get(job.id)
            finally:
                service.close()

        self.assertEqual("FAILED", recovered.stage)
        self.assertEqual("模型响应超时，请重新发起分析", recovered.detail)

    def test_snapshot_failure_has_a_clear_chinese_recovery_message(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))

            def broken_snapshot(_config: ConsoleConfig, _job_id: str) -> dict[str, object]:
                raise RuntimeError("private implementation detail")

            service = make_service(config, snapshot_runner=broken_snapshot)
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("FAILED", current.stage)
        self.assertEqual("无法读取 MT5 快照，请确认 MT5 已登录并保持运行", current.detail)
        self.assertNotIn("private", current.detail)

    def test_worker_failure_is_logged_to_runlog(self):
        """worker 里逃出 _run_job 的异常不得静默（future 捕获无人读），必须落 runlog。"""
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))

            class Boom(BaseException):
                pass

            def exploding_loader(_path):
                raise Boom("boom")

            service = make_service(
                config,
                market_data_runner=lambda _c, _j: (fake_snapshot(_c, _j), {"available": False}),
                event_loader=exploding_loader,
            )
            try:
                service.start("brief")
                deadline = time.monotonic() + 2
                content = ""
                while time.monotonic() < deadline:
                    if config.runlog_path.is_file():
                        content = config.runlog_path.read_text(encoding="utf-8")
                        if "job_error" in content:
                            break
                    time.sleep(0.02)
            finally:
                service.close()

        self.assertIn("job_error", content)
        self.assertIn("Boom", content)

    def test_brief_job_exposes_durable_stage_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
            )
            try:
                created = service.start("brief")
                self.assertEqual("QUEUED", created.stage)

                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        self.assertEqual(
            ["QUEUED", "SNAPSHOT", "GATE", "MODEL", "VALIDATE", "COMPLETE"],
            [event["stage"] for event in current.events],
        )
        self.assertEqual("报告已验收", current.detail)

    def test_gate_payload_carries_sensor_and_macro_status(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
            )
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        self.assertEqual(False, current.gate["tick_health"]["available"])
        self.assertEqual("unavailable", current.gate["macro_status"])
        self.assertEqual("verified_clear", current.gate["event_context"]["status"])
        self.assertEqual(PROMPT_VERSION, current.gate["prompt_version"])
        # 共振紧凑版随 gate 落盘（供复盘按情境聚合）；fake_snapshot 无结构 → 不可用但键存在
        self.assertIn("available", current.gate["resonance"])

    def test_facts_carry_timeframe_resonance_into_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            captured: dict[str, object] = {}

            def structured_snapshot(_config: ConsoleConfig, _job_id: str) -> dict[str, object]:
                return {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "identity_match": True,
                    "symbol": "XAUUSD",
                    "bid": 4000.0,
                    "ask": 4000.1,
                    "timeframe_structure": {
                        "m5": {"body_direction": "buy", "change_4": 1.0},
                        "m15": {"body_direction": "buy", "change_4": 2.0},
                        "h1": {"body_direction": "buy", "change_4": 10.0},
                        "h4": {"body_direction": "buy", "change_4": 30.0},
                    },
                }

            def capturing_brief(_config: ConsoleConfig, kind: str, facts: dict[str, object], gate: object, _mode: str = "scalp") -> object:
                captured["facts"] = facts
                return fake_brief(_config, kind, facts, gate, _mode)

            service = make_service(
                config,
                snapshot_runner=structured_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=capturing_brief,
            )
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        resonance = captured["facts"]["timeframe_resonance"]
        self.assertTrue(resonance["available"])
        self.assertEqual("共振偏多", resonance["label"])
        self.assertEqual(1.0, resonance["score"])
        # 同一份共振的紧凑版应随 gate 落盘，供复盘按情境聚合
        self.assertEqual("共振偏多", current.gate["resonance"]["label"])
        self.assertEqual(1.0, current.gate["resonance"]["score"])

    def test_review_stats_exposes_context_breakdown(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(config, snapshot_runner=fake_snapshot, brief_runner=fake_brief)
            try:
                job = service.store.create("brief")
                record = service.store.get(job.id)
                record.stage = "COMPLETE"
                record.report = {
                    "direction": "LONG",
                    "entry_zone": "3995-4005",
                    "take_profit": "4015",
                    "stop_loss": "3985",
                }
                record.gate = {
                    "action": "ANALYSE",
                    "prompt_version": PROMPT_VERSION,
                    "resonance": {"available": True, "score": 0.8, "label": "共振偏多"},
                }
                record.review = {"outcome": "TP_FIRST", "r_multiple": 1.0}
                service.store._write(record)

                stats = service.review_stats()
            finally:
                service.close()

        self.assertIn("contexts", stats)
        self.assertEqual(1.0, stats["contexts"]["by_gate_action"]["ANALYSE"]["win_rate"])
        self.assertEqual(1.0, stats["contexts"]["by_resonance"]["共振偏多"]["win_rate"])
        self.assertEqual(1.0, stats["contexts"]["by_direction"]["LONG"]["win_rate"])

    def test_sensor_failure_never_fails_the_job(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))

            def exploding_tick(_config: ConsoleConfig, _job_id: str) -> dict[str, object]:
                raise RuntimeError("sensor blew up")

            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
                tick_runner=exploding_tick,
            )
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        self.assertEqual(False, current.gate["tick_health"]["available"])

    def test_market_data_runner_is_called_once_and_replaces_separate_runners(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            calls = {"market": 0, "snapshot": 0, "tick": 0, "macro": 0}

            def counting_market(_config: ConsoleConfig, _job_id: str):
                calls["market"] += 1
                return fake_snapshot(_config, _job_id), {"available": True, "ticks": 10, "spread_max": 0.1, "stalled": False}

            def counting_snapshot(_config: ConsoleConfig, _job_id: str):
                calls["snapshot"] += 1
                return fake_snapshot(_config, _job_id)

            def counting_tick(_config: ConsoleConfig, job_id: str):
                if job_id != "self_check":  # 启动自检也会探针一次，不属于任务采集路径
                    calls["tick"] += 1
                return fake_tick_unavailable(_config, job_id)

            def counting_macro(_config: ConsoleConfig):
                calls["macro"] += 1
                return fake_macro_unavailable(_config)

            service = make_service(
                config,
                market_data_runner=counting_market,
                snapshot_runner=counting_snapshot,
                tick_runner=counting_tick,
                macro_runner=counting_macro,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
            )
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        # 杜绝重复调用：合并采集一次到位，独立快照/探针不再被调用，宏观层一次
        self.assertEqual(1, calls["market"])
        self.assertEqual(0, calls["snapshot"])
        self.assertEqual(0, calls["tick"])
        self.assertEqual(1, calls["macro"])

    def test_combined_failure_falls_back_to_separate_runners(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            calls = {"snapshot": 0, "tick": 0}

            def counting_snapshot(_config: ConsoleConfig, _job_id: str):
                calls["snapshot"] += 1
                return fake_snapshot(_config, _job_id)

            def counting_tick(_config: ConsoleConfig, job_id: str):
                if job_id != "self_check":  # 启动自检也会探针一次，不属于任务采集路径
                    calls["tick"] += 1
                return fake_tick_unavailable(_config, job_id)

            # 不注入 market_data_runner：合并采集因假路径立即失败，回退到独立 runner
            service = make_service(
                config,
                snapshot_runner=counting_snapshot,
                tick_runner=counting_tick,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
            )
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        self.assertEqual(1, calls["snapshot"])
        self.assertEqual(1, calls["tick"])

    def test_first_job_is_not_blocked_by_slow_startup_housekeeping(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            release_self_check = Event()

            def slow_self_check_tick(_config: ConsoleConfig, job_id: str):
                if job_id == "self_check":
                    release_self_check.wait(5)  # 模拟启动自检 MT5 探针很慢
                return fake_tick_unavailable(_config, job_id)

            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
                tick_runner=slow_self_check_tick,
            )
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                release_self_check.set()
                service.close()

        # 启动自检探针被阻塞期间，用户任务仍应在 2 秒内完成（家务不再排队阻塞首任务）
        self.assertEqual("COMPLETE", current.stage)


class AutoSchedulerTests(unittest.TestCase):
    """自主调度判定逻辑：四道闸门（未启用/节奏/活动任务/MT5离线）均不过则不发起分析。"""

    def build(self, directory: str) -> ConsoleService:
        config = make_config(Path(directory))
        service = make_service(
            config,
            snapshot_runner=fake_snapshot,
            event_loader=lambda _path: {"status": "verified_clear"},
            brief_runner=fake_brief,
        )
        # 停掉后台调度线程，手动驱动 _auto_tick 以保证确定性
        service._scheduler_stop.set()
        # 等启动自检落定（它只写一次 self_check_result），之后测试再改写才不会被覆盖
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and service.self_check_result.get("status") == "pending":
            time.sleep(0.01)
        return service

    def test_disabled_does_not_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.build(directory)
            try:
                service.auto_enabled = False
                self.assertEqual("disabled", service._auto_tick())
                self.assertEqual(0, len(service.store.list_recent()))
            finally:
                service.close()

    def test_interval_not_elapsed_does_not_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.build(directory)
            try:
                service.auto_enabled = True
                service.auto_interval_seconds = 9999
                service._last_auto_trigger = time.monotonic()
                self.assertEqual("interval", service._auto_tick())
                self.assertEqual(0, len(service.store.list_recent()))
            finally:
                service.close()

    def test_active_job_blocks_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.build(directory)
            try:
                service.store.create("brief")  # QUEUED = 活动任务
                service.auto_enabled = True
                service.auto_interval_seconds = 0
                service._last_auto_trigger = 0.0
                self.assertEqual("busy", service._auto_tick())
            finally:
                service.close()

    def test_mt5_offline_blocks_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.build(directory)
            try:
                service.auto_enabled = True
                service.auto_interval_seconds = 0
                service._last_auto_trigger = 0.0
                service.self_check_result = {"status": "done", "mt5": "offline"}
                self.assertEqual("offline", service._auto_tick())
                self.assertEqual(0, len(service.store.list_recent()))
            finally:
                service.close()

    def test_all_gates_pass_triggers_a_brief(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.build(directory)
            try:
                service.auto_enabled = True
                service.auto_interval_seconds = 0
                service._last_auto_trigger = 0.0
                service.self_check_result = {"status": "done", "mt5": "ok"}
                self.assertEqual("triggered", service._auto_tick())
                self.assertIsNotNone(service._last_auto_trigger_at)
                self.assertEqual(1, len(service.store.list_recent()))
            finally:
                service.close()

    def test_set_auto_enabled_updates_status(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.build(directory)
            try:
                status = service.set_auto_enabled(True)
                self.assertTrue(status["enabled"])
                self.assertEqual(service.auto_interval_seconds, status["interval_seconds"])
                self.assertFalse(service.set_auto_enabled(False)["enabled"])
            finally:
                service.close()

    def test_gate_payload_carries_news_status(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
                news_runner=fake_news_ok,
            )
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        self.assertEqual("ok", current.gate["news_status"])
        summary = current.gate["news_summary"]
        self.assertEqual(2, summary["count"])
        self.assertEqual("Gold surges on Fed cut", summary["items"][0]["title"])

    def test_news_injected_into_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            captured: dict[str, object] = {}

            def capturing_brief(_config: ConsoleConfig, kind: str, facts: dict[str, object], gate: object, _mode: str = "scalp") -> object:
                captured["facts"] = facts
                return fake_brief(_config, kind, facts, gate, _mode)

            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=capturing_brief,
                news_runner=fake_news_ok,
            )
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        news = captured["facts"]["news_context"]
        self.assertEqual("ok", news["status"])
        self.assertEqual(2, len(news["items"]))

    def test_news_runner_failure_degrades_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))

            def exploding_news(_config: ConsoleConfig) -> dict[str, object]:
                raise RuntimeError("news blew up")

            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
                news_runner=exploding_news,
            )
            try:
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        self.assertEqual("unavailable", current.gate["news_status"])
        self.assertIsNone(current.gate["news_summary"])

    def test_scheduler_loop_triggers_when_enabled_and_online(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
                tick_runner=lambda _c, _j: {"available": True},  # 自检会置 mt5=ok
            )
            try:
                service.auto_interval_seconds = 0
                service.auto_enabled = True
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and not service.store.list_recent():
                    time.sleep(0.05)
                self.assertGreaterEqual(len(service.store.list_recent()), 1)
            finally:
                service.close()

    def test_mt5_recovery_via_recheck_unblocks_scheduler(self):
        """MT5 启动时离线，恢复后重探针应解除 offline 闸门。"""
        with tempfile.TemporaryDirectory() as directory:
            service = self.build(directory)
            try:
                service.auto_enabled = True
                service.auto_interval_seconds = 0
                service._last_auto_trigger = 0.0
                service.self_check_result = {"status": "done", "mt5": "offline"}
                # 第一次：重探针间隔未到，应返回 offline
                service._last_mt5_recheck = time.monotonic()
                self.assertEqual("offline", service._auto_tick())
                # 模拟 MT5 恢复：tick_runner 返回 available=True
                service.tick_runner = lambda _c, _j: {"available": True}
                service._last_mt5_recheck = 0.0  # 强制重探针间隔已过
                self.assertEqual("triggered", service._auto_tick())
                self.assertEqual("ok", service.self_check_result["mt5"])
            finally:
                service.close()

    def test_mt5_recheck_still_offline_does_not_trigger(self):
        """MT5 重探针仍离线时不触发分析。"""
        with tempfile.TemporaryDirectory() as directory:
            service = self.build(directory)
            try:
                service.auto_enabled = True
                service.auto_interval_seconds = 0
                service._last_auto_trigger = 0.0
                service.self_check_result = {"status": "done", "mt5": "offline"}
                service._last_mt5_recheck = 0.0  # 强制重探针
                # tick_runner 仍返回不可用
                self.assertEqual("offline", service._auto_tick())
                self.assertEqual("offline", service.self_check_result["mt5"])
                self.assertEqual(0, len(service.store.list_recent()))
            finally:
                service.close()

    def test_hanging_context_fetch_does_not_block_job(self):
        """宏观/新闻拉取挂起时，任务应在超时后降级完成，不无限阻塞。"""
        import time as _time

        def hanging_macro(_config):
            _time.sleep(999)

        def hanging_news(_config):
            _time.sleep(999)

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=fake_brief,
                macro_runner=hanging_macro,
                news_runner=hanging_news,
            )
            try:
                with patch("local_console.service.CONTEXT_FETCH_TIMEOUT_SECONDS", 0.5):
                    started = _time.monotonic()
                    created = service.start("brief")
                    deadline = _time.monotonic() + 10
                    current = service.get(created.id)
                    while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and _time.monotonic() < deadline:
                        _time.sleep(0.05)
                        current = service.get(created.id)
                    elapsed = _time.monotonic() - started
            finally:
                service.close()

        self.assertIn(current.stage, {"COMPLETE", "REJECTED"}, f"任务应完成而非卡住，当前阶段：{current.stage}")
        self.assertLess(elapsed, 8.0, f"超时保护应快速降级，实际耗时 {elapsed:.1f}s")
        self.assertEqual("unavailable", current.gate["macro_status"])
        self.assertEqual("unavailable", current.gate["news_status"])

class ModeSwitchingTests(unittest.TestCase):
    """scalp/swing 双模式：前端切换 API + 每任务快照记录 mode。"""

    def test_default_mode_is_scalp_and_status_exposes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(config)
            try:
                status = service.mode_status()
            finally:
                service.close()
        self.assertEqual("scalp", status["mode"])

    def test_set_mode_switches_and_persists_in_service(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(config)
            try:
                status = service.set_mode("swing")
                self.assertEqual("swing", status["mode"])
                status2 = service.set_mode("scalp")
                self.assertEqual("scalp", status2["mode"])
            finally:
                service.close()

    def test_set_mode_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            service = make_service(config)
            try:
                with self.assertRaises(ValueError):
                    service.set_mode("grid")
            finally:
                service.close()

    def test_job_records_selected_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            captured: dict[str, object] = {}

            def capturing_brief(_config: ConsoleConfig, kind: str, facts: dict[str, object], gate: object, mode: str = "scalp") -> object:
                captured["mode"] = mode
                return fake_brief(_config, kind, facts, gate, mode)

            service = make_service(
                config,
                snapshot_runner=fake_snapshot,
                event_loader=lambda _path: {"status": "verified_clear"},
                brief_runner=capturing_brief,
            )
            try:
                service.set_mode("swing")
                created = service.start("brief")
                deadline = time.monotonic() + 2
                current = service.get(created.id)
                while current.stage not in {"COMPLETE", "REJECTED", "FAILED"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    current = service.get(created.id)
                record = service.get(created.id)
            finally:
                service.close()

        self.assertEqual("COMPLETE", current.stage)
        self.assertEqual("swing", record.mode)
        self.assertEqual("swing", captured["mode"])
