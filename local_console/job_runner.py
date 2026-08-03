"""Single-job execution orchestration (runs on the service's sole worker thread).

宏观背景、新闻背景与 MT5 快照相互独立：用守护线程并行获取，移出关键路径。
不用 ThreadPoolExecutor：其线程非 daemon，解释器退出/测试收尾时会 join
挂起的请求线程，把唯一 worker 与进程退出一起拖死（2026-08-01 实测挂起）。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

from .brief import PROMPT_VERSION, validate_report
from .config import ConsoleConfig
from .facts_builder import build_facts, build_gate_payload
from .housekeeping import run_reviews_safe
from .jobs import TERMINAL_STAGES
from .market_capture import capture_market_data, safe_ea_status, safe_macro, safe_news
from .runlog import log_event
from .session_context import (
    SESSION_CONTEXT_KEY,
    SESSION_LABEL_KEY,
    compute_session_context,
)
from .spread_percentile import compute_spread_percentile


def _persist_rejected_payload(
    config: ConsoleConfig, job_id: str, attempt: int, reason: str, payload: object
) -> None:
    """被拒报告的原始模型输出落盘，供事后诊断。

    审计发现（2026-08-03）：REJECTED 任务只留一句原因，原始输出完全黑盒，
    无法判断模型到底输出了什么（哪个字段/路径违规）。落盘到
    state_dir/rejected/{job_id}.jsonl，每行一次失败尝试；失败静默（诊断不阻断任务）。
    """
    try:
        directory = config.state_dir / "rejected"
        directory.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "job_id": job_id,
            "attempt": attempt,
            "reason": reason,
            "payload": payload,
        }
        path = directory / f"{job_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 诊断落盘失败不影响任务


def safe_iv(service: object) -> dict[str, object]:
    """IV 背景层与宏观/新闻同级：失败静默降级为不可用，绝不阻断任务。"""
    try:
        return service.iv_runner(service.config)
    except Exception:
        return {"status": "unavailable", "reason": "IV 波动层获取异常"}


def run_job(service: object, job_id: str) -> None:
    """Execute one job end to end; failures are mapped to FAILED stage."""
    try:
        context_results: dict[str, object] = {}
        context_threads: list[tuple[str, threading.Thread]] = []
        for key, runner in (
            ("macro", lambda: safe_macro(service.config, service.macro_runner)),
            ("news", lambda: safe_news(service.config, service.news_runner)),
            ("iv", lambda: safe_iv(service)),
        ):
            thread = threading.Thread(
                target=lambda k=key, fn=runner: context_results.__setitem__(k, fn()),
                name=f"xau-context-{key}",
                daemon=True,
            )
            thread.start()
            context_threads.append((key, thread))

        try:
            service._advance(job_id, "SNAPSHOT", "正在读取 MT5 快照与 tick 流")
            snapshot, tick_health = capture_market_data(
                service.config,
                job_id,
                market_data_runner=service.market_data_runner,
                snapshot_runner=service.snapshot_runner,
                tick_runner=service.tick_runner,
            )
            # A1 交易时段上下文（纯时间函数，失败安全）：注入快照，guard 据此
            # 给出非活跃时段流动性标注（guard.session_downgrade_reason 消费 label），
            # 完整上下文随 facts 进 prompt。
            session = compute_session_context()
            if session.get("status") == "ok":
                snapshot[SESSION_LABEL_KEY] = session["label"]
                snapshot[SESSION_CONTEXT_KEY] = session
            # A3 点差历史分位（读已落盘任务，失败安全）：样本不足如实返回 None。
            # 复用 service.store（同一把 JobStore 锁），不新建实例。
            tick_health["spread_percentile"] = compute_spread_percentile(
                tick_health.get("spread_median"), service.store
            )
            service._advance(
                job_id,
                "GATE",
                "正在校验快照时效、事件日历与市场传感器",
                snapshot=snapshot,
            )
            event_context = service.event_loader(service.config.event_context_path)
            macro = service._wait_context_result(
                context_threads, "macro", context_results, "宏观背景"
            )
            news = service._wait_context_result(
                context_threads, "news", context_results, "新闻"
            )
            iv_context = service._wait_context_result(
                context_threads, "iv", context_results, "IV 波动层"
            )
        finally:
            # 挂起的守护线程不阻塞任务与进程退出；它们只读，结果超时即被丢弃。
            pass

        ea_status = safe_ea_status(service.config, service.ea_status_runner)
        record = service.store.get(job_id)
        gate, gate_payload, resonance, regime = build_gate_payload(
            snapshot=snapshot,
            tick_health=tick_health,
            ea_status=ea_status,
            macro=macro,
            news=news,
            event_context=event_context,
            iv_context=iv_context,
            mode=record.mode,
        )
        log_event(
            service.config.runlog_path,
            kind="gate",
            job_id=job_id,
            action=gate.action,
            reason=gate.reason,
            prompt_version=PROMPT_VERSION,
        )
        if not gate.allow_model:
            service._advance(job_id, "COMPLETE", gate.reason, gate=gate_payload)
            return
        facts = build_facts(
            snapshot,
            macro=macro,
            tick_health=tick_health,
            event_context=event_context,
            resonance=resonance,
            regime=regime,
            news=news,
            iv_context=iv_context,
        )
        service._advance(job_id, "MODEL", "正在请求 Qwen 分析", gate=gate_payload)
        if record.kind == "deep_review":
            # 深度复盘：三家模型真辩论（Qwen/DeepSeek/GLM），讨论过程落盘供前端展示。
            from .debate import run_debate

            service._advance(job_id, "MODEL", "三家模型辩论中（Qwen/DeepSeek/GLM）", gate=gate_payload)
            # 辩论最坏 ~960s（4 轮 × 240s），远超陈旧阈值余量：心跳线程每 60s
            # 刷新 updated_at，防止陈旧扫描误杀仍在推进的辩论（2026-08-03 实测
            # 752 秒辩论被 750s 阈值误杀）。daemon 线程随辩论结束停止。
            heartbeat_stop = threading.Event()

            def _debate_heartbeat() -> None:
                while not heartbeat_stop.wait(60):
                    try:
                        service.store.touch(job_id, "三家模型辩论中（Qwen/DeepSeek/GLM）")
                    except Exception:
                        heartbeat_stop.set()
                        return

            heartbeat = threading.Thread(
                target=_debate_heartbeat, name=f"xau-debate-hb-{job_id[:8]}", daemon=True
            )
            heartbeat.start()
            try:
                debate_result = run_debate(service.config, facts, gate, record.mode)
            finally:
                heartbeat_stop.set()
            consensus = debate_result["consensus"]
            report = consensus.get("report")
            service._advance(job_id, "VALIDATE", "正在汇总辩论共识并校验", gate=gate_payload, debate=debate_result)
            if report is None:
                # 辩论全灭兜底（审计建议 6）：三家均未产出有效报告时降级为
                # 单模型深度分析，而非直接 REJECTED——集体故障不应让整个深度
                # 复盘作废；降级报告走同一验收门（宽松证据，与辩论一致）。
                valid_count = consensus.get("valid_count", 0)
                service._advance(job_id, "MODEL", f"辩论仅 {valid_count} 家有效，降级为单模型深度分析")
                payload = service.brief_runner(service.config, record.kind, facts, gate, record.mode)
                service._advance(job_id, "VALIDATE", "正在校验降级后的单模型深度分析")
                accepted, reason, fallback_report = validate_report(
                    payload, gate, facts, record.mode, loose_evidence=True
                )
                if accepted and fallback_report is not None:
                    fallback_report["debate"] = {
                        "fallback": True,
                        "rounds": len(debate_result["rounds"]),
                        "valid_count": valid_count,
                    }
                    service._advance(
                        job_id, "COMPLETE", "辩论降级为单模型深度分析并已验收",
                        report=fallback_report, debate=debate_result,
                    )
                else:
                    service._advance(
                        job_id, "REJECTED",
                        f"辩论失败：{valid_count} 家有效报告（需 ≥2 家）", debate=debate_result,
                    )
                run_reviews_safe(service, "post_job")
                return
            accepted, reason, validated = validate_report(report, gate, facts, record.mode, loose_evidence=True)
            if accepted and validated is not None:
                validated["debate"] = {
                    "rounds": len(debate_result["rounds"]),
                    "votes": report.get("debate_votes"),
                    "disagreements": report.get("debate_disagreements", []),
                }
                service._advance(job_id, "COMPLETE", "辩论共识报告已验收", report=validated, debate=debate_result)
            else:
                service._advance(job_id, "REJECTED", f"共识报告校验失败：{reason}", debate=debate_result)
        else:
            # 快评：DeepSeek 等模型偶发漏字段（summary/source_ids 等），
            # 验收失败时重试自愈（2026-08-03 余量：2 次重试 = 最多 3 次尝试，
            # 实测偶发坏 JSON/漏字段，重试一次不够，用户经常要手动点第二次）。
            accepted = False
            reason = ""
            report = None
            for attempt in range(3):
                payload = service.brief_runner(service.config, record.kind, facts, gate, record.mode)
                service._advance(job_id, "VALIDATE", "正在校验报告来源、数值与证据链")
                accepted, reason, report = validate_report(payload, gate, facts, record.mode)
                if accepted:
                    break
                # 校验未过：原始模型输出落盘，供事后诊断 REJECTED 根因
                # （审计发现：此前只留一句原因，模型到底输出了什么完全黑盒）。
                _persist_rejected_payload(service.config, job_id, attempt, reason, payload)
                if attempt < 2:
                    service._advance(job_id, "MODEL", "报告校验未过，重新生成一次", gate=gate_payload)
            service._advance(job_id, "COMPLETE" if accepted else "REJECTED", reason, report=report)
        # 任务收尾后补跑到期复盘（含本次之后到期的历史建议）
        run_reviews_safe(service, "post_job")
    except Exception as error:
        # 异常详情必须落 runlog（此前只记 stage 转换，异常丢失导致排查盲区）
        log_event(
            service.config.runlog_path,
            kind="job_error",
            job_id=job_id,
            error=f"{type(error).__name__}: {error}",
        )
        current = service.store.get(job_id)
        if current.stage in TERMINAL_STAGES:
            return
        detail = {
            "SNAPSHOT": "无法读取 MT5 快照，请确认 MT5 已登录并保持运行",
            "GATE": "无法校验市场事实，请重新发起分析",
            "MODEL": "模型分析失败，请稍后重新发起",
            "VALIDATE": "报告校验失败，请重新发起分析",
        }.get(current.stage, "分析任务失败，请重新发起")
        service._advance(job_id, "FAILED", detail)
