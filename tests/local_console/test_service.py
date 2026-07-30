from __future__ import annotations

import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from local_console.config import ConsoleConfig
from local_console.jobs import JobStore
from local_console.service import ConsoleService


def test_config(root: Path) -> ConsoleConfig:
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


def fake_brief(_config: ConsoleConfig, _kind: str, _snapshot: dict[str, object], _gate: object) -> object:
    return {
        "action": "ANALYSE",
        "source_ids": ["mt5_snapshot", "verified_event_context"],
        "summary": "快照可用于人工分析。",
        "invalidation": "后续快照会替代本次观察。",
        "next_observation": "下一根 M1 收盘后刷新。",
    }


class ConsoleServiceTests(unittest.TestCase):
    def test_start_reuses_the_active_job_instead_of_queueing_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            config = test_config(Path(directory))
            entered_model = Event()
            release_model = Event()

            def slow_brief(
                brief_config: ConsoleConfig,
                kind: str,
                snapshot: dict[str, object],
                gate: object,
            ) -> object:
                entered_model.set()
                release_model.wait(1)
                return fake_brief(brief_config, kind, snapshot, gate)

            service = ConsoleService(
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
            config = test_config(Path(directory))
            stale_store = JobStore(config.jobs_dir)
            job = stale_store.create("brief")
            record = stale_store.get(job.id)
            record.stage = "MODEL"
            record.updated_at = "2000-01-01T00:00:00+00:00"
            stale_store._write(record)

            service = ConsoleService(config)
            try:
                recovered = service.get(job.id)
            finally:
                service.close()

        self.assertEqual("FAILED", recovered.stage)

    def test_brief_job_exposes_durable_stage_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            config = test_config(Path(directory))
            service = ConsoleService(
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
