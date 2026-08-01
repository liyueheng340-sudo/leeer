"""Background orchestration for local, read-only XAU analysis jobs."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable

from .brief import (
    PROMPT_VERSION,
    request_brief,
    validate_report,
    worst_case_seconds,
)
from .calendar import evaluate_calendar, load_event_context
from .config import ConsoleConfig
from .ea_status import load_ea_status
from .guard import GateResult, evaluate_gate
from .jobs import TERMINAL_STAGES, JobKind, JobRecord, JobStore
from .macro import fetch_macro_background
from .news import fetch_news_context
from .resonance import compute_resonance
from .review import compute_context_stats, compute_review_stats, run_due_reviews
from .runlog import log_event
from .snapshot import SnapshotCaptureError, capture_combined, capture_snapshot
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
# 数据保留策略：终态任务与其快照默认保留 30 天；无论多旧至少保留最新 200 条。
RETENTION_MAX_AGE_SECONDS = 30 * 24 * 3600
RETENTION_KEEP_MINIMUM = 200
# 自主调度守护线程的轮询粒度（与采样节奏解耦）：每秒醒一次判定闸门，
# 是否真发起分析由 auto_interval_seconds 控制。
AUTO_POLL_SECONDS = 1.0
# MT5 启动时离线后，自主调度每 60 秒轻量探针一次，恢复在线后无需重启控制台。
MT5_RECHECK_INTERVAL_SECONDS = 60.0
# 宏观/新闻背景拉取的最大等待时间。超时后降级为 unavailable，不阻塞关键路径。
CONTEXT_FETCH_TIMEOUT_SECONDS = 20.0
SnapshotRunner = Callable[[ConsoleConfig, str], dict[str, object]]
EventLoader = Callable[[Path], dict[str, object]]
BriefRunner = Callable[[ConsoleConfig, JobKind, dict[str, object], GateResult], object]
TickRunner = Callable[[ConsoleConfig, str], dict[str, object]]
MacroRunner = Callable[[ConsoleConfig], dict[str, object]]
NewsRunner = Callable[[ConsoleConfig], dict[str, object]]
EaStatusRunner = Callable[[ConsoleConfig], dict[str, object]]
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
            target=self._startup_housekeeping, name="xau-housekeeping", daemon=True
        )
        self._housekeeping_thread.start()
        # 自主调度（默认关，交易员显式开启）：独立守护线程按节奏采样，
        # 同样不占用唯一 worker；_auto_tick 经四道闸门后才发起分析。
        self.auto_enabled = config.auto_enabled_default
        self.auto_interval_seconds = config.auto_interval_seconds
        self._last_auto_trigger = 0.0  # monotonic：0 表示从未触发
        self._last_auto_trigger_at: str | None = None  # 墙钟 ISO，供前端展示
        self._last_mt5_recheck = 0.0  # monotonic：上次 MT5 重探针时间
        self._scheduler_stop = Event()
        self._scheduler_thread = Thread(
            target=self._scheduler_loop, name="xau-scheduler", daemon=True
        )
        self._scheduler_thread.start()

    def start(self, kind: JobKind) -> JobRecord:
        with self._start_lock:
            self._fail_stale_jobs_throttled(force=True)
            active = next(
                (record for record in self.store.list_recent() if record.stage not in TERMINAL_STAGES),
                None,
            )
            if active is not None:
                return active
            record = self.store.create(kind)
            future = self.executor.submit(self._run_job, record.id)
            future.add_done_callback(self._log_worker_failure)
            return record

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
        # 情境复盘：同一批记录按单维度切分，回答“什么情境下流程有 edge”。
        stats["contexts"] = compute_context_stats(records)
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
        if self._has_active_job():
            return "busy"
        if self.self_check_result.get("mt5") != "ok":
            # MT5 启动时离线不代表永远离线：节流重探针，恢复后无需重启控制台。
            if now - self._last_mt5_recheck >= MT5_RECHECK_INTERVAL_SECONDS:
                self._last_mt5_recheck = now
                tick = self._safe_tick_health("recheck")
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
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _fail_stale_jobs_throttled(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_stale_scan < STALE_SCAN_INTERVAL_SECONDS:
            return
        self._last_stale_scan = now
        self.store.fail_stale_jobs(STALE_THRESHOLD_SECONDS)

    def _startup_housekeeping(self) -> None:
        """启动自检（MT5 / FRED / 日历）+ 补跑到期复盘。

        运行在独立守护线程：绝不占用用户任务的唯一 worker，首个任务无需排队。
        MT5 采集为独立子进程且只读，与用户任务偶尔并发亦安全；_wait_for_idle
        仅在活动任务期间让位以减少终端负载竞争。
        """
        try:
            if not self._wait_for_idle():
                return
            tick = self._safe_tick_health("self_check")
            calendar_state = evaluate_calendar(self.config.calendar_path)
            self.self_check_result = {
                "status": "done",
                "mt5": "ok" if tick.get("available") is True else "offline",
                "fred": "configured" if os.environ.get("FRED_API_KEY") else "missing_key",
                "calendar": (
                    "fresh" if calendar_state.get("status") in ("verified_clear", "wait") else "stale"
                ),
                "checked_at": datetime.now(UTC).isoformat(),
            }
            log_event(self.config.runlog_path, kind="self_check", **self.self_check_result)
        except Exception:
            self.self_check_result = {"status": "error"}
        if self._wait_for_idle():
            self._run_reviews_safe("startup")
            self._prune_old_data()

    def _prune_old_data(self) -> None:
        """按保留期清理过期任务记录与 MT5 快照，避免状态目录无限增长。

        只触碰过期文件（进行中的任务与其快照均为新近文件），与前台任务无竞争；
        任何失败都只记日志，不影响服务。
        """
        try:
            pruned_jobs = self.store.prune_expired(RETENTION_MAX_AGE_SECONDS, RETENTION_KEEP_MINIMUM)
            for job_id in pruned_jobs:
                (self.config.snapshots_dir / f"{job_id}.jsonl").unlink(missing_ok=True)
            pruned_snapshots = self._prune_old_snapshots()
            if pruned_jobs or pruned_snapshots:
                log_event(
                    self.config.runlog_path,
                    kind="retention",
                    pruned_jobs=len(pruned_jobs),
                    pruned_snapshots=pruned_snapshots,
                )
        except Exception:
            log_event(self.config.runlog_path, kind="retention", error=True)

    def _prune_old_snapshots(self) -> int:
        """按文件修改时间清理过期快照（含无对应任务的孤儿文件）。"""
        if not self.config.snapshots_dir.is_dir():
            return 0
        now = time.time()
        removed = 0
        for path in self.config.snapshots_dir.glob("*.jsonl"):
            try:
                if now - path.stat().st_mtime > RETENTION_MAX_AGE_SECONDS:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    def _wait_for_idle(self) -> bool:
        """等待直到没有进行中的用户任务；收到停止信号时返回 False。"""
        while not self._housekeeping_stop.is_set():
            if not self._has_active_job():
                return True
            self._housekeeping_stop.wait(1.0)
        return False

    def _has_active_job(self) -> bool:
        return any(
            record.stage not in TERMINAL_STAGES for record in self.store.list_recent()
        )

    def _run_reviews_safe(self, trigger: str) -> None:
        """复盘是后台测量动作，任何失败都不影响前台任务。"""
        try:
            written = self.review_runner(self.config, self.store)
            if written:
                log_event(self.config.runlog_path, kind="review", trigger=trigger, written=written)
        except Exception:
            log_event(self.config.runlog_path, kind="review", trigger=trigger, error=True)

    def _capture_market_data(self, job_id: str) -> tuple[dict[str, object], dict[str, object]]:
        """一次 MT5 会话产出快照与 tick 健康；合并采集失败时回退到两个独立脚本。"""
        if self.market_data_runner is not None:
            return self.market_data_runner(self.config, job_id)
        try:
            return capture_combined(self.config, job_id)
        except SnapshotCaptureError:
            snapshot = self.snapshot_runner(self.config, job_id)
            tick_health = self._safe_tick_health(job_id)
            return snapshot, tick_health

    def _safe_tick_health(self, job_id: str) -> dict[str, object]:
        """tick 传感器是辅助降级触发器，故障时静默降级为不可用，绝不阻断任务。"""
        try:
            return self.tick_runner(self.config, job_id)
        except Exception:
            return {"available": False, "reason": "tick 传感器异常"}

    def _safe_macro(self) -> dict[str, object]:
        """宏观背景层同理：不可用只是少一层背景，不是任务失败。"""
        try:
            return self.macro_runner(self.config)
        except Exception:
            return {"status": "unavailable", "reason": "宏观背景获取异常"}

    def _safe_news(self) -> dict[str, object]:
        """新闻背景层同理：不可用只是少一层预期差参考，不是任务失败。"""
        try:
            return self.news_runner(self.config)
        except Exception:
            return {"status": "unavailable", "reason": "新闻背景获取异常"}

    def _safe_ea_status(self) -> dict[str, object]:
        """EA 风控态与 tick 传感器同级：辅助降级触发器，故障时静默不可用，绝不阻断。"""
        try:
            return self.ea_status_runner(self.config)
        except Exception:
            return {"available": False, "reason": "EA 状态读取异常"}

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

    def _run_job(self, job_id: str) -> None:
        try:
            # 宏观背景、新闻背景与 MT5 快照相互独立：用守护线程并行获取，移出关键路径。
            # 不用 ThreadPoolExecutor：其线程非 daemon，解释器退出/测试收尾时会 join
            # 挂起的请求线程，把唯一 worker 与进程退出一起拖死（2026-08-01 实测挂起）。
            context_results: dict[str, object] = {}
            context_threads: list[tuple[str, threading.Thread]] = []
            for key, runner in (("macro", self._safe_macro), ("news", self._safe_news)):
                thread = threading.Thread(
                    target=lambda k=key, fn=runner: context_results.__setitem__(k, fn()),
                    name=f"xau-context-{key}",
                    daemon=True,
                )
                thread.start()
                context_threads.append((key, thread))

            try:
                self._advance(job_id, "SNAPSHOT", "正在读取 MT5 快照与 tick 流")
                snapshot, tick_health = self._capture_market_data(job_id)
                self._advance(
                    job_id,
                    "GATE",
                    "正在校验快照时效、事件日历与市场传感器",
                    snapshot=snapshot,
                )
                event_context = self.event_loader(self.config.event_context_path)
                macro = self._wait_context_result(context_threads, "macro", context_results, "宏观背景")
                news = self._wait_context_result(context_threads, "news", context_results, "新闻")
            finally:
                # 挂起的守护线程不阻塞任务与进程退出；它们只读，结果超时即被丢弃。
                pass

            ea_status = self._safe_ea_status()
            gate = evaluate_gate(snapshot, event_context, datetime.now(UTC), tick_health, ea_status)
            # 共振只算一次：紧凑版随 gate 落盘（供复盘按情境聚合），完整版喂给模型。
            resonance = compute_resonance(snapshot)
            gate_payload = {
                **gate.to_dict(),
                "tick_health": tick_health,
                # EA 风控态只落风险机制字段（不含持仓/盈亏），供前端与复盘追溯降级来源。
                "ea_status": (
                    {
                        "status": ea_status.get("status"),
                        "regime_blocked": ea_status.get("regime_blocked"),
                        "hour_blocked": ea_status.get("hour_blocked"),
                        "feed": ea_status.get("feed"),
                        "age_seconds": ea_status.get("age_seconds"),
                    }
                    if ea_status.get("available") is True
                    else None
                ),
                "macro_status": macro.get("status"),
                "macro_summary": (
                    {
                        sid: {
                            "label": item.get("label"),
                            "latest": item.get("latest"),
                            "change_recent": item.get("change_recent"),
                            "date": item.get("date"),
                        }
                        for sid, item in macro.get("series", {}).items()
                    }
                    if macro.get("status") == "ok"
                    else None
                ),
                "event_context": event_context,
                "prompt_version": PROMPT_VERSION,
                "news_status": news.get("status"),
                "news_summary": (
                    {
                        "count": len(news.get("items", [])),
                        "items": [
                            {
                                "title": item.get("title"),
                                "topic": item.get("topic"),
                                "publisher": item.get("publisher"),
                                "utc": item.get("utc"),
                            }
                            for item in news.get("items", [])[:5]
                            if isinstance(item, dict)
                        ],
                    }
                    if news.get("status") == "ok"
                    else None
                ),
                "resonance": {
                    "available": resonance.get("available"),
                    "score": resonance.get("score"),
                    "label": resonance.get("label"),
                },
            }
            log_event(
                self.config.runlog_path,
                kind="gate",
                job_id=job_id,
                action=gate.action,
                reason=gate.reason,
                prompt_version=PROMPT_VERSION,
            )
            if not gate.allow_model:
                self._advance(job_id, "COMPLETE", gate.reason, gate=gate_payload)
                return
            record = self.store.get(job_id)
            # 模型事实包 = MT5 快照 + 宏观背景层 + tick 传感器读数 + 事件日历
            facts = dict(snapshot)
            facts["background_macro"] = macro
            facts["tick_health"] = tick_health
            facts["event_context"] = event_context
            facts["timeframe_resonance"] = resonance
            facts["news_context"] = news
            self._advance(job_id, "MODEL", "正在请求 Qwen 分析", gate=gate_payload)
            payload = self.brief_runner(self.config, record.kind, facts, gate)
            self._advance(job_id, "VALIDATE", "正在校验报告来源、数值与证据链")
            accepted, reason, report = validate_report(payload, gate, facts)
            self._advance(job_id, "COMPLETE" if accepted else "REJECTED", reason, report=report)
            # 任务收尾后补跑到期复盘（含本次之后到期的历史建议）
            self._run_reviews_safe("post_job")
        except Exception:
            current = self.store.get(job_id)
            if current.stage in TERMINAL_STAGES:
                return
            detail = {
                "SNAPSHOT": "无法读取 MT5 快照，请确认 MT5 已登录并保持运行",
                "GATE": "无法校验市场事实，请重新发起分析",
                "MODEL": "Qwen 分析失败，请稍后重新发起",
                "VALIDATE": "报告校验失败，请重新发起分析",
            }.get(current.stage, "分析任务失败，请重新发起")
            self._advance(job_id, "FAILED", detail)
