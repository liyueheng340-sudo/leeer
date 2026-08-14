"""Background orchestration for local, read-only XAU analysis jobs."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread

from .brief import (
    request_brief,
    worst_case_seconds,
)
from .calendar import load_event_context
from .config import ConsoleConfig
from .ea_status import load_ea_status
from .guard import GateResult
from .housekeeping import (
    has_active_job,
    run_startup_housekeeping,
)
from .iv import fetch_iv_context
from .job_runner import run_job
from .jobs import TERMINAL_STAGES, JobKind, JobMode, JobRecord, JobStore
from .macro import fetch_macro_background
from .market_capture import (
    safe_tick_health,
)
from .news import fetch_news_context
from .review import (
    compute_context_stats,
    compute_direction_quality,
    compute_forward_validation,
    compute_review_stats,
    run_due_reviews,
)
from .runlog import log_event
from .snapshot import capture_snapshot
from .ticks import capture_tick_health

# 前端每秒轮询任务状态；陈旧任务扫描是全目录遍历，节流到每 2 秒一次
STALE_SCAN_INTERVAL_SECONDS = 2.0
# 陈旧任务判定阈值必须大于模型最坏耗时，否则一次正常的慢分析会被误判为超时失败。
# 取快评与深度复盘两种任务最坏耗时的最大值（深度复盘用推理模型，更慢），
# 再留 30 秒余量给快照/闸门/校验与调度抖动。
STALE_MARGIN_SECONDS = 30
STALE_THRESHOLD_SECONDS = (
    max(worst_case_seconds("brief"), worst_case_seconds("deep_review"))
    + STALE_MARGIN_SECONDS
)
# 自主调度守护线程的轮询粒度（与采样节奏解耦）：每秒醒一次判定闸门，
# 是否真发起分析由 auto_interval_seconds 控制。
AUTO_POLL_SECONDS = 1.0
# MT5 启动时离线后，自主调度每 60 秒轻量探针一次，恢复在线后无需重启控制台。
MT5_RECHECK_INTERVAL_SECONDS = 60.0
# 宏观/新闻背景拉取的最大等待时间。超时后降级为 unavailable，不阻塞关键路径。
CONTEXT_FETCH_TIMEOUT_SECONDS = 20.0
SnapshotRunner = Callable[[ConsoleConfig, str], dict[str, object]]
EventLoader = Callable[[Path], dict[str, object]]
BriefRunner = Callable[[ConsoleConfig, JobKind, dict[str, object], GateResult, str], object]
TickRunner = Callable[[ConsoleConfig, str], dict[str, object]]
MacroRunner = Callable[[ConsoleConfig], dict[str, object]]
NewsRunner = Callable[[ConsoleConfig], dict[str, object]]
EaStatusRunner = Callable[[ConsoleConfig], dict[str, object]]
IvRunner = Callable[[ConsoleConfig], dict[str, object]]
MarketDataRunner = Callable[[ConsoleConfig, str], tuple[dict[str, object], dict[str, object]]]
ReviewRunner = Callable[[ConsoleConfig, JobStore], int]


class ConsoleService:
    def __init__(
        self,
        config: ConsoleConfig,
        *,
        snapshot_runner: SnapshotRunner = capture_snapshot,
        event_loader: EventLoader | None = None,
        brief_runner: BriefRunner = request_brief,
        tick_runner: TickRunner = capture_tick_health,
        macro_runner: MacroRunner = fetch_macro_background,
        news_runner: NewsRunner = fetch_news_context,
        market_data_runner: MarketDataRunner | None = None,
        review_runner: ReviewRunner = run_due_reviews,
        ea_status_runner: EaStatusRunner = load_ea_status,
        iv_runner: IvRunner | None = None,
    ):
        self.config = config
        self.store = JobStore(config.jobs_dir)
        self.store.fail_stale_jobs(STALE_THRESHOLD_SECONDS)
        self.snapshot_runner = snapshot_runner
        # 默认事件来源：手工覆写优先的自动事件日历（calendar.py）
        self.event_loader = event_loader or (lambda _path: load_event_context(config))
        self.brief_runner = brief_runner
        self.tick_runner = tick_runner
        self.macro_runner = macro_runner
        self.news_runner = news_runner
        self.market_data_runner = market_data_runner
        self.review_runner = review_runner
        self.ea_status_runner = ea_status_runner
        self.iv_runner = iv_runner or fetch_iv_context
        self.self_check_result: dict[str, object] = {"status": "pending"}
        self._start_lock = Lock()
        self._last_stale_scan = 0.0
        # 单个本地 worker 串行执行用户任务：进度诚实，且避免用户任务之间重叠读取 MT5。
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xau-analysis")
        # 启动家务移到独立守护线程：不再占用唯一 worker，首个用户任务无需排队等待。
        # MT5 采集均为独立子进程且只读，家务与用户任务即使偶尔并发也安全；
        # 家务仅通过 _wait_for_idle 在活动任务期间让位（减少终端负载竞争，非硬互斥）。
        self._housekeeping_stop = Event()
        self._housekeeping_thread = Thread(
            target=lambda: run_startup_housekeeping(self),
            name="xau-housekeeping",
            daemon=True,
        )
        self._housekeeping_thread.start()
        # 自主调度（默认关，交易员显式开启）：独立守护线程按节奏采样，
        # 同样不占用唯一 worker；_auto_tick 经四道闸门后才发起分析。
        self.auto_enabled = config.auto_enabled_default
        self.auto_interval_seconds = config.auto_interval_seconds
        self._last_auto_trigger = 0.0  # monotonic：0 表示从未触发
        self._last_auto_trigger_at: str | None = None  # 墙钟 ISO，供前端展示
        self._last_mt5_recheck = 0.0  # monotonic：上次 MT5 重探针时间
        # 交易模式：scalp（剥头皮）/ swing（日内波段）。前端一键切换，每任务快照记录。
        self.mode: JobMode = "scalp"
        self._scheduler_stop = Event()
        self._scheduler_thread = Thread(
            target=self._scheduler_loop, name="xau-scheduler", daemon=True
        )
        self._scheduler_thread.start()
        # 2026-08-04 审查修复：运行中长任务（辩论）的 abort 事件注册表。
        # close() 广播 set 让 run_debate 轮间检查提前返回，配合 wait=True
        # 保证 shutdown 快速完成（既避免生产 Ctrl+C 卡死，也保持测试确定性）。
        self._abort_events: set[Event] = set()
        self._abort_lock = Lock()

    def register_abort(self, event: Event) -> None:
        """注册运行中任务的中止事件（job_runner 辩论心跳失败时创建）。"""
        with self._abort_lock:
            self._abort_events.add(event)

    def unregister_abort(self, event: Event) -> None:
        with self._abort_lock:
            self._abort_events.discard(event)

    def _broadcast_abort(self) -> None:
        with self._abort_lock:
            for event in self._abort_events:
                event.set()

    def start(self, kind: JobKind) -> JobRecord:
        with self._start_lock:
            self._fail_stale_jobs_throttled(force=True)
            active = next(
                (record for record in self.store.list_recent() if record.stage not in TERMINAL_STAGES),
                None,
            )
            if active is not None:
                return active
            record = self.store.create(kind, self.mode)
            future = self.executor.submit(run_job, self, record.id)
            future.add_done_callback(self._log_worker_failure)
            return record

    def mode_status(self) -> dict[str, object]:
        """当前交易模式（前端切换控件数据源）。"""
        return {"mode": self.mode}

    def set_mode(self, mode: str) -> dict[str, object]:
        """切换 scalp / swing 交易模式；影响之后发起的所有任务。"""
        if mode not in {"scalp", "swing"}:
            raise ValueError("交易模式必须是 scalp 或 swing")
        self.mode = mode  # type: ignore[assignment]
        log_event(self.config.runlog_path, kind="mode", mode=self.mode)
        return self.mode_status()

    def _log_worker_failure(self, future: object) -> None:
        """worker 任务异常必须可见：concurrent.futures 会把任务异常收进 future，
        无人读取则彻底无痕——2026-07-31 卡单事故中任务因此静默停在 QUEUED。"""
        try:
            error = future.exception()  # type: ignore[attr-defined]
        except Exception:
            return
        if error is not None:
            log_event(
                self.config.runlog_path,
                kind="job_error",
                error=f"{type(error).__name__}: {error}",
            )

    def get(self, job_id: str) -> JobRecord:
        self._fail_stale_jobs_throttled()
        return self.store.get(job_id)

    def history(self) -> list[JobRecord]:
        self._fail_stale_jobs_throttled()
        return self.store.list_recent()

    def review_stats(self) -> dict[str, object]:
        records = self.store.list_recent(limit=200)
        stats = compute_review_stats(records)
        # 情境复盘：同一批记录按单维度切分，回答"什么情境下流程有 edge"。
        stats["contexts"] = compute_context_stats(records)
        # 前向验证：最近窗口 vs 更早窗口，回答"纪律生效后 edge 是否持续/改善"。
        stats["forward_validation"] = compute_forward_validation(records)
        # 方向×结果四分格（P0）：区分方向能力 vs 执行点位能力。
        stats["direction_quality"] = compute_direction_quality(records)
        return stats

    def auto_status(self) -> dict[str, object]:
        """自主调度的当前状态（开关 / 节奏 / 上次触发时间）。"""
        return {
            "enabled": self.auto_enabled,
            "interval_seconds": self.auto_interval_seconds,
            "last_trigger_at": self._last_auto_trigger_at,
        }

    def set_auto_enabled(self, enabled: bool) -> dict[str, object]:
        """交易员显式开/关自主调度；仅内存态，重启回到配置默认。"""
        self.auto_enabled = bool(enabled)
        log_event(self.config.runlog_path, kind="auto", enabled=self.auto_enabled)
        return self.auto_status()

    def _scheduler_loop(self) -> None:
        """自主调度守护线程：按轮询粒度醒来看闸门，绝不占用用户任务的唯一 worker。"""
        while not self._scheduler_stop.wait(AUTO_POLL_SECONDS):
            try:
                self._auto_tick()
            except Exception:
                log_event(self.config.runlog_path, kind="auto", error=True)

    def _auto_tick(self) -> str:
        """一次调度判定，返回原因（供日志与测试）；仅过四道闸门才发起分析。

        闸门顺序（任一不过即不跑，这是反刷屏的核心）：
        未启用 → 节奏未到 → 有活动任务 → MT5 离线。
        """
        if not self.auto_enabled:
            return "disabled"
        now = time.monotonic()
        if now - self._last_auto_trigger < self.auto_interval_seconds:
            return "interval"
        if has_active_job(self):
            return "busy"
        if self.self_check_result.get("mt5") != "ok":
            # MT5 启动时离线不代表永远离线：节流重探针，恢复后无需重启控制台。
            if now - self._last_mt5_recheck >= MT5_RECHECK_INTERVAL_SECONDS:
                self._last_mt5_recheck = now
                tick = safe_tick_health(self.config, "recheck", self.tick_runner)
                if tick.get("available") is True:
                    self.self_check_result = {**self.self_check_result, "mt5": "ok"}
                    log_event(self.config.runlog_path, kind="self_check", mt5="ok", trigger="recheck")
                else:
                    return "offline"
            else:
                return "offline"  # 数据源离线时不刷失败任务
        self._last_auto_trigger = now
        self._last_auto_trigger_at = datetime.now(UTC).isoformat()
        log_event(
            self.config.runlog_path,
            kind="auto_trigger",
            interval_seconds=self.auto_interval_seconds,
        )
        self.start("brief")
        return "triggered"

    def close(self) -> None:
        # 先停止并回收启动家务线程与调度线程，避免它们在服务关闭后仍访问状态目录。
        self._scheduler_stop.set()
        self._scheduler_thread.join(timeout=5)
        self._housekeeping_stop.set()
        self._housekeeping_thread.join(timeout=5)
        # 2026-08-04 审查修复（STRUCT）：此前 wait=True + cancel_futures=False 会让
        # close() 在深度复盘运行时阻塞最多 ~960s（辩论最坏），Ctrl+C 期间进程无法退出。
        # 修复：1) 广播 abort 让运行中辩论轮间检查提前返回；2) 标记运行中任务 FAILED
        # （陈旧扫描语义，避免重启后任务卡 MODEL）；3) 再 wait=True 等 worker 收尾。
        self._broadcast_abort()
        for record in self.store.list_recent():
            if record.stage not in TERMINAL_STAGES:
                with contextlib.suppress(OSError, ValueError):
                    # 关闭路径尽力而为，失败不阻塞退出
                    self.store.transition(record.id, "FAILED", "服务关闭，任务中断")
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _fail_stale_jobs_throttled(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_stale_scan < STALE_SCAN_INTERVAL_SECONDS:
            return
        self._last_stale_scan = now
        self.store.fail_stale_jobs(STALE_THRESHOLD_SECONDS)

    def _wait_context_result(
        self,
        threads: list[tuple[str, threading.Thread]],
        key: str,
        results: dict[str, object],
        label: str,
    ) -> dict[str, object]:
        """带超时等待后台守护线程结果；超时降级为 unavailable，绝不阻塞唯一 worker。"""
        deadline = time.monotonic() + CONTEXT_FETCH_TIMEOUT_SECONDS
        while key not in results:
            if time.monotonic() >= deadline:
                return {"status": "unavailable", "reason": f"{label}获取超时"}
            time.sleep(0.05)
        return results[key]

    def _advance(self, job_id: str, stage: str, detail: str, **updates: object) -> JobRecord:
        """Stage transition + one structured run-log line."""
        record = self.store.transition(job_id, stage, detail, **updates)  # type: ignore[arg-type]
        log_event(
            self.config.runlog_path,
            kind="stage",
            job_id=job_id,
            stage=stage,
            detail=detail,
            elapsed_seconds=record.elapsed_seconds,
        )
        return record

