"""Durable job records used by the browser progress view."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
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

    def create(self, kind: JobKind) -> JobRecord:
        if kind not in {"brief", "deep_review"}:
            raise ValueError(f"unsupported job kind: {kind}")
        now = utc_now()
        return self._write(JobRecord(uuid4().hex, kind, "QUEUED", now, now, "Queued"))

    def get(self, job_id: str) -> JobRecord:
        path = self._path(job_id)
        if not path.is_file():
            raise KeyError(job_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("elapsed_seconds", None)
        return JobRecord(**payload)

    def list_recent(self, limit: int = 20) -> list[JobRecord]:
        records = [self.get(path.stem) for path in self.root.glob("*.json")]
        return sorted(records, key=lambda record: record.created_at, reverse=True)[:limit]

    def transition(self, job_id: str, stage: JobStage, detail: str, **updates: object) -> JobRecord:
        if stage not in VALID_STAGES:
            raise ValueError(f"unsupported job stage: {stage}")
        record = self.get(job_id)
        if record.stage in TERMINAL_STAGES:
            raise ValueError("terminal jobs cannot transition")
        record.stage = stage
        record.detail = detail
        record.updated_at = utc_now()
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
