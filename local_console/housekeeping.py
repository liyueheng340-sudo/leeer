"""Startup housekeeping: self-check, due reviews, and data retention.

运行在独立守护线程，绝不占用用户任务的唯一 worker；MT5 采集为独立子进程
且只读，与用户任务偶尔并发亦安全，仅在活动任务期间让位以减少终端负载竞争。
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

from .calendar import evaluate_calendar
from .jobs import TERMINAL_STAGES
from .market_capture import safe_tick_health
from .runlog import log_event

# 数据保留策略：终态任务与其快照默认保留 30 天；无论多旧至少保留最新 200 条。
RETENTION_MAX_AGE_SECONDS = 30 * 24 * 3600
RETENTION_KEEP_MINIMUM = 200


def run_startup_housekeeping(service: object) -> None:
    """启动自检（MT5 / FRED / 日历）+ 补跑到期复盘（在独立守护线程执行）。"""
    try:
        if not wait_for_idle(service):
            return
        tick = safe_tick_health(service.config, "self_check", service.tick_runner)
        calendar_state = evaluate_calendar(service.config.calendar_path)
        # 日历状态细分：文件缺失（长期拉取失败）与文件过期分开呈现——
        # missing 红灯（前端 lamp bad），stale 黄灯（前端 lamp warn）。
        calendar_file_missing = not service.config.calendar_path.is_file()
        calendar_status = (
            "fresh" if calendar_state.get("status") in ("verified_clear", "wait")
            else "missing" if calendar_file_missing
            else "stale"
        )
        service.self_check_result = {
            "status": "done",
            "mt5": "ok" if tick.get("available") is True else "offline",
            "fred": "configured" if os.environ.get("FRED_API_KEY") else "missing_key",
            "calendar": calendar_status,
            "checked_at": datetime.now(UTC).isoformat(),
        }
        log_event(service.config.runlog_path, kind="self_check", **service.self_check_result)
    except Exception:
        service.self_check_result = {"status": "error"}
    if wait_for_idle(service):
        run_reviews_safe(service, "startup")
        prune_old_data(service)


def wait_for_idle(service: object) -> bool:
    """等待直到没有进行中的用户任务；收到停止信号时返回 False。"""
    while not service._housekeeping_stop.is_set():
        if not has_active_job(service):
            return True
        service._housekeeping_stop.wait(1.0)
    return False


def has_active_job(service: object) -> bool:
    return any(
        record.stage not in TERMINAL_STAGES for record in service.store.list_recent()
    )


def run_reviews_safe(service: object, trigger: str) -> None:
    """复盘是后台测量动作，任何失败都不影响前台任务。"""
    try:
        written = service.review_runner(service.config, service.store)
        if written:
            log_event(service.config.runlog_path, kind="review", trigger=trigger, written=written)
    except Exception:
        log_event(service.config.runlog_path, kind="review", trigger=trigger, error=True)


def prune_old_data(service: object) -> None:
    """按保留期清理过期任务记录与 MT5 快照，避免状态目录无限增长。

    只触碰过期文件（进行中的任务与其快照均为新近文件），与前台任务无竞争；
    任何失败都只记日志，不影响服务。
    """
    try:
        pruned_jobs = service.store.prune_expired(
            RETENTION_MAX_AGE_SECONDS, RETENTION_KEEP_MINIMUM
        )
        for job_id in pruned_jobs:
            (service.config.snapshots_dir / f"{job_id}.jsonl").unlink(missing_ok=True)
        pruned_snapshots = prune_old_snapshots(service)
        if pruned_jobs or pruned_snapshots:
            log_event(
                service.config.runlog_path,
                kind="retention",
                pruned_jobs=len(pruned_jobs),
                pruned_snapshots=pruned_snapshots,
            )
    except Exception:
        log_event(service.config.runlog_path, kind="retention", error=True)


def prune_old_snapshots(service: object) -> int:
    """按文件修改时间清理过期快照（含无对应任务的孤儿文件）。"""
    if not service.config.snapshots_dir.is_dir():
        return 0
    now = time.time()
    removed = 0
    for path in service.config.snapshots_dir.glob("*.jsonl"):
        try:
            if now - path.stat().st_mtime > RETENTION_MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
