from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

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

    def test_stale_scan_tolerates_corrupt_files(self):
        # 损坏文件不得让陈旧扫描崩溃（否则全部 API 与服务启动都会被单文件拖垮）。
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job = store.create("brief")
            record = store.get(job.id)
            record.stage = "MODEL"
            record.updated_at = "2026-07-30T00:00:00+00:00"
            store._write(record)
            (Path(directory) / "broken1.json").write_text("{not json", encoding="utf-8")
            (Path(directory) / "broken2.json").write_text('{"foo": 1}', encoding="utf-8")

            recovered = store.fail_stale_jobs(
                90, now=datetime(2026, 7, 30, 0, 2, tzinfo=UTC)
            )

            self.assertEqual(1, recovered)
            self.assertEqual("FAILED", store.get(job.id).stage)
            # 损坏文件不阻塞历史列表（含非法 schema 的 TypeError 路径）
            self.assertEqual(1, len(store.list_recent()))

    def test_prune_removes_old_terminal_jobs_but_keeps_recent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            old = store.create("brief")
            recent = store.create("brief")
            store.transition(old.id, "COMPLETE", "done")
            store.transition(recent.id, "COMPLETE", "done")
            record = store.get(old.id)
            record.updated_at = "2026-01-01T00:00:00+00:00"
            store._write(record)

            pruned = store.prune_expired(90, keep_minimum=0, now=datetime(2026, 7, 30, tzinfo=UTC))

            self.assertEqual([old.id], pruned)
            self.assertFalse(store._path(old.id).exists())
            self.assertTrue(store._path(recent.id).exists())

    def test_prune_keeps_minimum_even_when_old(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            ids = []
            for index in range(3):
                job = store.create("brief")
                store.transition(job.id, "COMPLETE", "done")
                record = store.get(job.id)
                record.updated_at = "2026-01-01T00:00:00+00:00"
                store._write(record)
                mtime = 1_000_000 + index  # index 越大 mtime 越新
                os.utime(store._path(job.id), (mtime, mtime))
                ids.append(job.id)

            pruned = store.prune_expired(90, keep_minimum=2, now=datetime(2026, 7, 30, tzinfo=UTC))

            # 全部超过保留期，但保底保留最新 2 条，只删最旧的 ids[0]
            self.assertEqual([ids[0]], pruned)
            self.assertFalse(store._path(ids[0]).exists())
            self.assertTrue(store._path(ids[1]).exists())
            self.assertTrue(store._path(ids[2]).exists())

    def test_prune_never_removes_active_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            active = store.create("brief")  # QUEUED 非终态
            record = store.get(active.id)
            record.updated_at = "2026-01-01T00:00:00+00:00"
            store._write(record)

            pruned = store.prune_expired(90, keep_minimum=0, now=datetime(2026, 7, 30, tzinfo=UTC))

            self.assertEqual([], pruned)
            self.assertTrue(store._path(active.id).exists())

    def test_write_retries_permission_error_and_recovers(self):
        # Windows 下外部进程瞬时占用目标文件会让 os.replace 抛 PermissionError；
        # 退避重试后写入必须成功，不得吞掉阶段推进（2026-07-31 卡单根因）。
        original_replace = Path.replace
        calls = {"count": 0}

        def flaky_replace(self: Path, target: Path) -> None:
            calls["count"] += 1
            if calls["count"] <= 2:
                raise PermissionError(5, "拒绝访问")
            return original_replace(self, target)

        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            record = store.get(store.create("brief").id)
            record.stage = "SNAPSHOT"
            with patch.object(Path, "replace", flaky_replace), patch("time.sleep"):
                store._write(record)

            restored = JobStore(Path(directory)).get(record.id)

        self.assertEqual("SNAPSHOT", restored.stage)
        self.assertEqual(3, calls["count"])

    def test_write_falls_back_to_direct_write_when_replace_keeps_failing(self):
        def always_denied(self: Path, target: Path) -> None:
            raise PermissionError(5, "拒绝访问")

        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            record = store.get(store.create("brief").id)
            record.stage = "GATE"
            with patch.object(Path, "replace", always_denied), patch("time.sleep"):
                store._write(record)

            payload = json.loads(store._path(record.id).read_text(encoding="utf-8"))

        self.assertEqual("GATE", payload["stage"])

    def test_init_cleans_orphan_tmp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            stray = Path(directory) / "deadbeef.tmp"
            stray.write_text("{}", encoding="utf-8")

            JobStore(Path(directory))

            self.assertFalse(stray.exists())

    def test_stale_scan_tolerates_single_write_failure(self):
        # 单个陈旧任务的写入失败（如外部进程瞬时占用）不得拖垮扫描与全部 API；
        # 该任务跳过，其余陈旧任务照常回收（2026-08-01 卡单事故的回归保护）。
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            stuck = store.create("brief")
            record = store.get(stuck.id)
            record.stage = "QUEUED"
            record.updated_at = "2026-07-30T00:00:00+00:00"
            store._write(record)
            healthy = store.create("brief")
            record2 = store.get(healthy.id)
            record2.stage = "MODEL"
            record2.updated_at = "2026-07-30T00:00:00+00:00"
            store._write(record2)

            original_transition = store.transition

            def denied_transition(job_id, stage, detail, **updates):
                if job_id == stuck.id:
                    raise PermissionError(5, "拒绝访问")
                return original_transition(job_id, stage, detail, **updates)

            store.transition = denied_transition  # type: ignore[method-assign]
            try:
                recovered = store.fail_stale_jobs(
                    90, now=datetime(2026, 7, 30, 0, 2, tzinfo=UTC)
                )
            finally:
                store.transition = original_transition

            self.assertEqual(1, recovered)
            self.assertEqual("QUEUED", store.get(stuck.id).stage)
            self.assertEqual("FAILED", store.get(healthy.id).stage)
