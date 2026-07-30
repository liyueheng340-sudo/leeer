"""Background orchestration for local, read-only XAU analysis jobs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .brief import request_brief, validate_report
from .config import ConsoleConfig
from .guard import GateResult, evaluate_gate, load_event_context
from .jobs import JobKind, JobRecord, JobStore
from .snapshot import capture_snapshot

MODEL_TIMEOUT_SECONDS = 90
SnapshotRunner = Callable[[ConsoleConfig, str], dict[str, object]]
EventLoader = Callable[[Path], dict[str, object]]
BriefRunner = Callable[[ConsoleConfig, JobKind, dict[str, object], GateResult], object]


class ConsoleService:
    def __init__(
        self,
        config: ConsoleConfig,
        *,
        snapshot_runner: SnapshotRunner = capture_snapshot,
        event_loader: EventLoader = load_event_context,
        brief_runner: BriefRunner = request_brief,
    ):
        self.config = config
        self.store = JobStore(config.jobs_dir)
        self.store.fail_stale_jobs(MODEL_TIMEOUT_SECONDS)
        self.snapshot_runner = snapshot_runner
        self.event_loader = event_loader
        self.brief_runner = brief_runner
        # ponytail: one local worker makes progress honest and avoids overlapping MT5 reads.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xau-analysis")

    def start(self, kind: JobKind) -> JobRecord:
        record = self.store.create(kind)
        self.executor.submit(self._run_job, record.id)
        return record

    def get(self, job_id: str) -> JobRecord:
        return self.store.get(job_id)

    def history(self) -> list[JobRecord]:
        return self.store.list_recent()

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _run_job(self, job_id: str) -> None:
        try:
            self.store.transition(job_id, "SNAPSHOT", "正在读取 MT5 快照")
            snapshot = self.snapshot_runner(self.config, job_id)
            self.store.transition(
                job_id,
                "GATE",
                "正在校验数据时效与事件上下文",
                snapshot=snapshot,
            )
            gate = evaluate_gate(snapshot, self.event_loader(self.config.event_context_path), datetime.now(UTC))
            if not gate.allow_model:
                self.store.transition(job_id, "COMPLETE", gate.reason, gate=gate.to_dict())
                return
            record = self.store.get(job_id)
            self.store.transition(job_id, "MODEL", "正在请求 Qwen 分析", gate=gate.to_dict())
            payload = self.brief_runner(self.config, record.kind, snapshot, gate)
            self.store.transition(job_id, "VALIDATE", "正在校验报告来源与约束")
            accepted, reason, report = validate_report(payload, gate)
            self.store.transition(job_id, "COMPLETE" if accepted else "REJECTED", reason, report=report)
        except Exception:
            self.store.transition(job_id, "FAILED", "分析任务失败")
