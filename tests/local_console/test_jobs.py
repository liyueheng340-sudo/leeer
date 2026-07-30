from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from local_console.jobs import JobStore


class JobStoreTests(unittest.TestCase):
    def test_persists_transition_across_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            first = JobStore(Path(directory))
            created = first.create("brief")
            first.transition(created.id, "SNAPSHOT", "正在读取 MT5 快照")

            restored = JobStore(Path(directory)).get(created.id)

        self.assertEqual("SNAPSHOT", restored.stage)
        self.assertEqual("正在读取 MT5 快照", restored.detail)
        self.assertGreaterEqual(restored.elapsed_seconds, 0)
        self.assertEqual(["QUEUED", "SNAPSHOT"], [event["stage"] for event in restored.events])

    def test_rejects_transition_from_terminal_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job = store.create("brief")
            store.transition(job.id, "COMPLETE", "Done")

            with self.assertRaisesRegex(ValueError, "terminal"):
                store.transition(job.id, "MODEL", "Must not restart")

    def test_stale_active_job_is_failed_on_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job = store.create("brief")
            record = store.get(job.id)
            record.stage = "MODEL"
            record.updated_at = "2026-07-30T00:00:00+00:00"
            store._write(record)

            recovered = store.fail_stale_jobs(
                90, now=datetime(2026, 7, 30, 0, 2, tzinfo=UTC)
            )
            result = store.get(job.id)

        self.assertEqual(1, recovered)
        self.assertEqual("FAILED", result.stage)
        self.assertEqual("模型响应超时，请重新发起分析", result.detail)
