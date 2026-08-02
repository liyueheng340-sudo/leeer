"""Single-job execution orchestration (runs on the service's sole worker thread).

宏观背景、新闻背景与 MT5 快照相互独立：用守护线程并行获取，移出关键路径。
不用 ThreadPoolExecutor：其线程非 daemon，解释器退出/测试收尾时会 join
挂起的请求线程，把唯一 worker 与进程退出一起拖死（2026-08-01 实测挂起）。
"""

from __future__ import annotations

import threading

from .brief import PROMPT_VERSION, validate_report
from .facts_builder import build_facts, build_gate_payload
from .housekeeping import run_reviews_safe
from .jobs import TERMINAL_STAGES
from .market_capture import capture_market_data, safe_ea_status, safe_macro, safe_news
from .runlog import log_event


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
        gate, gate_payload, resonance, regime = build_gate_payload(
            snapshot=snapshot,
            tick_health=tick_health,
            ea_status=ea_status,
            macro=macro,
            news=news,
            event_context=event_context,
            iv_context=iv_context,
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
        record = service.store.get(job_id)
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
            debate_result = run_debate(service.config, facts, gate, record.mode)
            consensus = debate_result["consensus"]
            report = consensus.get("report")
            service._advance(job_id, "VALIDATE", "正在汇总辩论共识并校验", gate=gate_payload, debate=debate_result)
            if report is None:
                reason = f"辩论失败：{consensus.get('valid_count', 0)} 家有效报告（需 ≥2 家）"
                service._advance(job_id, "REJECTED", reason, debate=debate_result)
                run_reviews_safe(service, "post_job")
                return
            accepted, reason, validated = validate_report(report, gate, facts, record.mode)
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
            payload = service.brief_runner(service.config, record.kind, facts, gate, record.mode)
            service._advance(job_id, "VALIDATE", "正在校验报告来源、数值与证据链")
            accepted, reason, report = validate_report(payload, gate, facts, record.mode)
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
