"""Durable job records used by the browser progress view."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

JobKind = Literal["brief", "deep_review"]
JobStage = Literal[
    "QUEUED",
    "SNAPSHOT",
    "GATE",
    "MODEL",
    "VALIDATE",
    "COMPLETE",
    "REJECTED",
    "FAILED",
]

TERMINAL_STAGES = {"COMPLETE", "REJECTED", "FAILED"}
VALID_STAGES = {
    "QUEUED",
    "SNAPSHOT",
    "GATE",
    "MODEL",
    "VALIDATE",
    *TERMINAL_STAGES,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class JobRecord:
    id: str
    kind: JobKind
    stage: JobStage
    created_at: str
    updated_at: str
    detail: str
    snapshot: dict[str, object] | None = None
    gate: dict[str, object] | None = None
    report: dict[str, object] | None = None
    events: list[dict[str, str]] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> float:
        started = datetime.fromisoformat(self.created_at)
        return max(0.0, (datetime.now(UTC) - started).total_seconds())

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["elapsed_seconds"] = round(self.elapsed_seconds, 1)
        return result


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._transition_lock = RLock()

    def create(self, kind: JobKind) -> JobRecord:
        if kind not in {"brief", "deep_review"}:
            raise ValueError(f"unsupported job kind: {kind}")
        now = utc_now()
        return self._write(
            JobRecord(
                uuid4().hex,
                kind,
                "QUEUED",
                now,
                now,
                "任务已创建",
                events=[{"stage": "QUEUED", "at": now, "detail": "任务已创建"}],
            )
        )

    def get(self, job_id: str) -> JobRecord:
        path = self._path(job_id)
        if not path.is_file():
            raise KeyError(job_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("elapsed_seconds", None)
        payload.setdefault("events", [])
        return JobRecord(**payload)

    def list_recent(self, limit: int = 20) -> list[JobRecord]:
        records = [self.get(path.stem) for path in self.root.glob("*.json")]
        return sorted(records, key=lambda record: record.created_at, reverse=True)[:limit]

    def fail_stale_jobs(self, max_age_seconds: float, now: datetime | None = None) -> int:
        with self._transition_lock:
            reference = now or datetime.now(UTC)
            failed = 0
            for path in self.root.glob("*.json"):
                record = self.get(path.stem)
                if record.stage in TERMINAL_STAGES:
                    continue
                updated = datetime.fromisoformat(record.updated_at)
                if (reference - updated).total_seconds() <= max_age_seconds:
                    continue
                self.transition(record.id, "FAILED", "模型响应超时，请重新发起分析")
                failed += 1
            return failed

    def transition(self, job_id: str, stage: JobStage, detail: str, **updates: object) -> JobRecord:
        with self._transition_lock:
            if stage not in VALID_STAGES:
                raise ValueError(f"unsupported job stage: {stage}")
            record = self.get(job_id)
            if record.stage in TERMINAL_STAGES:
                raise ValueError("terminal jobs cannot transition")
            record.stage = stage
            record.detail = detail
            record.updated_at = utc_now()
            record.events.append({"stage": stage, "at": record.updated_at, "detail": detail})
            for key, value in updates.items():
                if key not in {"snapshot", "gate", "report"}:
                    raise ValueError(f"unsupported job update: {key}")
                setattr(record, key, value)
            return self._write(record)

    def _path(self, job_id: str) -> Path:
        if not job_id.isalnum():
            raise KeyError(job_id)
        return self.root / f"{job_id}.json"

    def _write(self, record: JobRecord) -> JobRecord:
        path = self._path(record.id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return record
