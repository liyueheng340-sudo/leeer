"""Durable job records used by the browser progress view."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

# Windows 下 os.replace 会被外部进程（Defender 实时扫描/索引器等）瞬时占用目标文件
# 而抛 PermissionError(WinError 5)：短退避重试即可自愈——2026-07-31 两次"任务卡
# QUEUED"事故的根因就是该异常吞掉了阶段写入，且补救性的 FAILED 写入同样被撞。
_WRITE_MAX_ATTEMPTS = 6
_WRITE_BACKOFF_SECONDS = 0.1

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
    review: dict[str, object] | None = None
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
        # 启动即清理历史事故遗留的 .tmp 孤儿（replace 失败残留，永远不是有效记录）。
        for stray in self.root.glob("*.tmp"):
            try:
                stray.unlink()
            except OSError:
                pass

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
        # 读路径同样进入可重入锁：与 _write 的原子替换串行化，
        # 避免家务线程与任务线程并发读写同一 JSON 文件（Windows 下会 PermissionError）。
        with self._transition_lock:
            path = self._path(job_id)
            if not path.is_file():
                raise KeyError(job_id)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("elapsed_seconds", None)
            payload.setdefault("events", [])
            return JobRecord(**payload)

    def list_recent(self, limit: int = 20) -> list[JobRecord]:
        """按文件修改时间倒序，只解析足够数量的最新记录，避免全目录逐文件读取。"""
        with self._transition_lock:
            entries = sorted(
                (path for path in self.root.glob("*.json") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            records: list[JobRecord] = []
            for path in entries:
                if len(records) >= limit:
                    break
                try:
                    records.append(self.get(path.stem))
                except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                    continue  # 跳过损坏文件，不阻塞历史列表
            return sorted(records, key=lambda record: record.created_at, reverse=True)[:limit]

    def fail_stale_jobs(self, max_age_seconds: float, now: datetime | None = None) -> int:
        with self._transition_lock:
            reference = now or datetime.now(UTC)
            failed = 0
            for path in self.root.glob("*.json"):
                try:
                    record = self.get(path.stem)
                except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                    continue  # 损坏文件不属于陈旧任务；跳过而非让全部 API 崩溃（清理由 prune_expired 负责）
                if record.stage in TERMINAL_STAGES:
                    continue
                updated = datetime.fromisoformat(record.updated_at)
                if (reference - updated).total_seconds() <= max_age_seconds:
                    continue
                try:
                    self.transition(record.id, "FAILED", "模型响应超时，请重新发起分析")
                except (OSError, ValueError):
                    # 单个陈旧任务的写入失败（如外部进程瞬时占用）不得拖垮全部 API：
                    # 跳过本任务继续扫描，下一轮再试；其余任务照常回收。
                    # （2026-08-01 事故：一个文件写失败让 status/history 全部 500，
                    #   且该任务保持 QUEUED，start() 永远复用活动任务而无法开新任务。）
                    continue
                failed += 1
            return failed

    def prune_expired(
        self,
        max_age_seconds: float,
        keep_minimum: int = 200,
        now: datetime | None = None,
    ) -> list[str]:
        """删除超过保留期的终态任务记录，返回被删除的 job_id。

        无论多旧，至少保留最新的 keep_minimum 条，避免误删全部历史；
        进行中的任务永不清理。损坏文件在超出最小保留数后一并清除。
        """
        with self._transition_lock:
            reference = now or datetime.now(UTC)
            paths = sorted(
                (path for path in self.root.glob("*.json") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            pruned: list[str] = []
            for index, path in enumerate(paths):
                if index < keep_minimum:
                    continue  # 保护最新的 keep_minimum 条，不论年龄
                try:
                    record = self.get(path.stem)
                except (KeyError, ValueError, json.JSONDecodeError):
                    path.unlink(missing_ok=True)  # 损坏文件直接清理
                    pruned.append(path.stem)
                    continue
                if record.stage not in TERMINAL_STAGES:
                    continue  # 进行中的任务永不 pruning
                updated = datetime.fromisoformat(record.updated_at)
                if (reference - updated).total_seconds() <= max_age_seconds:
                    continue
                path.unlink(missing_ok=True)
                pruned.append(record.id)
            return pruned

    def set_review(self, job_id: str, review: dict[str, object]) -> JobRecord:
        """Attach a post-hoc review to a (usually terminal) job without a stage transition."""
        with self._transition_lock:
            record = self.get(job_id)
            record.review = review
            return self._write(record)

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
        # 写路径进入同一把可重入锁：保证原子替换期间没有并发读取。
        with self._transition_lock:
            path = self._path(record.id)
            temporary = path.with_suffix(".tmp")
            payload = json.dumps(record.to_dict(), ensure_ascii=False, indent=2)
            last_error: OSError | None = None
            for attempt in range(_WRITE_MAX_ATTEMPTS):
                try:
                    temporary.write_text(payload, encoding="utf-8")
                    temporary.replace(path)
                    return record
                except (PermissionError, FileNotFoundError) as error:
                    # PermissionError：外部进程瞬时占用目标文件（Windows 常见，退避后自愈）；
                    # FileNotFoundError：同名 .tmp 被挪走的极端竞争，下一轮重写即可。
                    last_error = error
                    time.sleep(_WRITE_BACKOFF_SECONDS * (attempt + 1))
            # 兜底：原子替换持续失败时退化为直接写（牺牲原子性换可用性；读端有锁
            # 保护，外部读到半写文件的概率极低，且读取层本就容忍损坏文件）。
            try:
                path.write_text(payload, encoding="utf-8")
            except OSError:
                if last_error is not None:
                    raise last_error
                raise
            return record
