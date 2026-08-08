"""Three-model debate orchestration for deep reviews.

深度复盘 = 三家模型（Qwen / DeepSeek / GLM）真辩论：
- 第 1 轮：三家并行独立分析（各带视角定位：技术 / 宏观情绪 / 风险对抗），
  每家输出完整报告契约（validate_report 同款 schema）；
- 第 2 轮：三家并行交叉辩论——看到前一轮另两家的观点与报告，输出
  "坚持 / 修正 / 分歧点" 的立场声明；
- 第 3 轮（分歧大时自动）：分歧收敛轮，输出最终立场；
- 共识层：方向投票（≥2 家一致）→ 分歧清单 → 综合报告（由票数多的一方
  主笔，附分歧说明），综合报告过 validate_report 验收。

容错：单家失败不影响辩论（≥2 家成功即可出综合报告）；每家报告独立验收，
失败/无效的报告剔除。深度复盘超时阈值放宽（推理模型 thinking 慢）。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tradingagents.llm_clients.factory import create_llm_client

from .config import ConsoleConfig
from .guard import GateResult
from .prompt_rules import build_debate_prompt
from .report_validation import validate_report

# 每家调用的 token 预算：推理模型 thinking 会消耗大量 token，必须给足
# 否则 content 为空（finish_reason=length）导致 JSON 解析失败（实测根因）。
DEBATE_MAX_TOKENS = 8000
DEBATE_TIMEOUT_SECONDS = 240
# 辩论最大轮数：第 1 轮独立 + 第 2 轮交叉 + 分歧时第 3 轮收敛。
# worst_case_seconds 用它与 DEBATE_TIMEOUT_SECONDS 计算陈旧阈值上限。
DEBATE_MAX_ROUNDS = 3
# 三家并行；单家失败不阻断（≥2 家有效即出综合）
DEBATE_MIN_VALID = 2
# 单家瞬时失败（网络抖动/连接错误）重试次数。
DEBATE_RETRIES = 1

# 辩论模型阵容：视角定位决定第 1 轮 prompt 的侧重。
# 三家都走同一阿里云兼容端点（DASHSCOPE_API_KEY），provider 统一用 "qwen"
# （qwen 的 registry spec 会读 DASHSCOPE_API_KEY + backend_url）。
# 不能用 deepseek/openai_compatible：前者要求 DEEPSEEK_API_KEY（.env 未配置），
# 后者不传 key（401）——这正是此前深度复盘必败的根因。
# 注意：qwen3.8-max-preview 在完整报告契约 prompt 下 content 恒空
# （纯 reasoning 模型，实测 4000/8000 token 均无输出），技术派改用 qwen3.7-max。
# persona（2026-08-07 人格注入）：给三家互补的投资哲学视角强化观点质量，
# 仅作分析视角增强，不引入任何 JSON 输出字段 / 不改变 required_json_keys。
DEBATE_TEAM: list[dict[str, str]] = [
    {"provider": "qwen", "model": "qwen3.7-max", "role": "技术面主攻",
     "focus": "多周期结构、共振、关键价位、点位纪律（技术派视角）",
     "persona": "量化纪律派（Paul Tudor Jones 式）：顺势而为、严格截断亏损、让利润奔跑。"
                "强调结构共振与关键价位的几何纪律，严格执行 ATR 止损，宁可错过不错做；"
                "任何进场都必须有可复算的价位依据，绝不追价、绝不在无结构处凭感觉押注。"},
    {"provider": "qwen", "model": "deepseek-v4-flash-0731", "role": "宏观情绪主攻",
     "focus": "宏观背景、新闻预期差、事件驱动（宏观派视角）",
     "persona": "宏观对冲派（Ray Dalio 经济机器式）：锚定实际利率、美元、流动性与通胀预期的传导链，"
                "突出黄金的货币与避险属性。关注事件预期差——价格是否已定价，而非反应式追新闻；"
                "用宏观周期判断中期方向，再落到当下价位。"},
    {"provider": "qwen", "model": "glm-5.2", "role": "风险对抗",
     "focus": "专挑前两者逻辑漏洞与风险盲区（风控派视角）",
     "persona": "安全边际派（Benjamin Graham 式）：第一性质疑前两者假设是否经得起验证。"
                "紧盯仓位与尾部风险、宏观数据的时滞缺口、新闻尚未定价的盲区；"
                "当机会看似完美时，主动寻找被忽视的失败前提，宁可保守也不承担不对称的下行风险。"},
]


def _invoke_model(config: ConsoleConfig, provider: str, model: str, prompt: str) -> str:
    """调用单家模型返回原始文本；瞬时失败（网络抖动/连接错误）重试有限次。

    推理模型 thinking 后 content 可能仍为空（token 耗尽）则报错，由调用方剔除。
    双区端点冗余（与 brief 一致）：主端点失败/额度耗尽时切备用端点重试，
    避免深度复盘因单一套餐耗尽而整场失败（2026-08-07 增强）。
    """
    last_error: Exception | None = None
    for attempt in range(DEBATE_RETRIES + 1):
        try:
            llm = create_llm_client(
                provider, model, config.backend_url,
                timeout=DEBATE_TIMEOUT_SECONDS, max_retries=0,
            ).get_llm()
            response = llm.invoke(prompt, max_tokens=DEBATE_MAX_TOKENS)
            content = getattr(response, "content", response)
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError(f"{model} 返回空内容（推理 token 耗尽）")
            return content
        except Exception as error:
            last_error = error
            # 主端点失败 → 备用端点重试一次（阿里云双区独立计费）
            if config.fallback_backend_url and config.fallback_api_key:
                try:
                    fallback_llm = create_llm_client(
                        "openai_compatible", model, config.fallback_backend_url,
                        api_key=config.fallback_api_key, timeout=DEBATE_TIMEOUT_SECONDS,
                        max_retries=0,
                    ).get_llm()
                    response = fallback_llm.invoke(prompt, max_tokens=DEBATE_MAX_TOKENS)
                    content = getattr(response, "content", response)
                    if isinstance(content, str) and content.strip():
                        return content
                except Exception:
                    pass  # 备用也失败：继续走主端点重试/抛原始错误，便于诊断
            if attempt < DEBATE_RETRIES:
                time.sleep(2)
    raise RuntimeError(f"{model} 调用失败") from last_error


def _run_parallel(
    config: ConsoleConfig,
    jobs: list[tuple[str, str, str]],  # (provider, model, prompt)
) -> list[tuple[str, str, str, str | None]]:
    """并行调用多家，返回 [(provider, model, prompt, content_or_error)]。"""
    results: list[tuple[str, str, str, str | None]] = []

    def _one(pair: tuple[str, str, str]) -> None:
        provider, model, prompt = pair
        try:
            content = _invoke_model(config, provider, model, prompt)
            results.append((provider, model, prompt, content))
        except Exception as error:
            results.append((provider, model, prompt, f"ERROR: {type(error).__name__}: {error}"))

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        list(pool.map(_one, jobs))
    return results


def _parse_report(content: str) -> object:
    """宽松 JSON 解析（复用 brief 的容错逻辑：围栏/夹带文字）。"""
    import re

    text = content.strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as error:
            last_error = error
    raise RuntimeError("模型输出不是 JSON") from last_error


def _digest_report(report: dict[str, Any] | None) -> str:
    """把已验收报告压缩成结构化摘要（供第 2/3 轮交叉引用）。

    只传核心字段而非全文：token 成本降低、响应更快，且辩论聚焦于
    方向/价位/理由，而不是互相复读长文（不重复调用的核心优化）。
    """
    if not report:
        return "（无有效报告）"
    summary = str(report.get("summary") or "")[:80]
    parts = [
        f"方向={report.get('direction', '—')}",
        f"入场={report.get('entry_zone', '—')}",
        f"止盈={report.get('take_profit', '—')}",
        f"止损={report.get('stop_loss', '—')}",
    ]
    if summary:
        parts.append(f"逻辑={summary}")
    return "；".join(parts)


def _aborted_result(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """辩论被外部中止（心跳失败/服务关闭）时返回当前进度 + 空共识。

    调用方（job_runner）看到 consensus.report is None 且 valid_count=0，
    会走辩论失败降级路径（单模型深度分析兜底），不会把中止误当成功。
    """
    return {
        "rounds": rounds,
        "consensus": {
            "direction": None,
            "report": None,
            "valid_count": 0,
            "aborted": True,
        },
    }


def run_debate(
    config: ConsoleConfig,
    snapshot: dict[str, object],
    gate: GateResult,
    mode: str,
    max_rounds: int = 3,
    abort_event: threading.Event | None = None,
) -> dict[str, Any]:
    """执行三家辩论，返回完整讨论记录 + 综合报告。

    返回结构：
        rounds: [{round: 1, statements: [{provider, model, role, content, ok}]}]
        consensus: {direction, votes, disagree: [...], report, valid_count}

    abort_event（2026-08-04 审查修复）：外部（心跳失败/服务关闭）置位时，
    每轮调用前检查并提前返回当前进度——避免辩论在心跳已失效时盲跑至结果
    被陈旧扫描丢弃（valid_count=0 → 调用方走失败路径）。
    """
    rounds: list[dict[str, Any]] = []

    def _aborted() -> bool:
        return abort_event is not None and abort_event.is_set()

    # 第 1 轮：独立分析（各带视角定位 + 人格注入）
    round1_prompts = [
        (t["provider"], t["model"],
         build_debate_prompt(snapshot, gate, mode, t["role"], t["focus"],
                             round_no=1, persona=t.get("persona")))
        for t in DEBATE_TEAM
    ]
    if _aborted():
        return _aborted_result(rounds)
    round1 = _run_parallel(config, round1_prompts)
    # 每家的报告（解析 + 独立验收）
    reports: dict[str, dict[str, Any]] = {}
    statements_1: list[dict[str, Any]] = []
    for provider, model, _prompt, content in round1:
        ok = False
        report_payload: dict[str, Any] | None = None
        if content and not content.startswith("ERROR"):
            try:
                payload = _parse_report(content)
                valid, reason, report = validate_report(payload, gate, snapshot, mode, loose_evidence=True)
                ok = valid
                if valid:
                    report_payload = report  # type: ignore[assignment]
                    reports[f"{provider}:{model}"] = report  # type: ignore[assignment]
            except Exception:
                ok = False
        statements_1.append({
            "provider": provider, "model": model,
            "role": next(t["role"] for t in DEBATE_TEAM if t["model"] == model),
            "content": content if not content.startswith("ERROR") else None,
            "error": content if content.startswith("ERROR") else None,
            "ok": ok,
            "digest": _digest_report(report_payload),
        })
    rounds.append({"round": 1, "statements": statements_1})

    # 修复轮：第 1 轮 <2 家有效时，让失败的模型按提示补全（提升容错，防单家小失误拖垮整场）
    if len(reports) < DEBATE_MIN_VALID and not _aborted():
        repair_prompts = []
        for t in DEBATE_TEAM:
            provider, model = t["provider"], t["model"]
            if f"{provider}:{model}" in reports:
                continue  # 已有效，不重复调用
            repair_prompts.append(
                (provider, model,
                 build_debate_prompt(snapshot, gate, mode, t["role"], t["focus"],
                                     round_no=1, others=statements_1,
                                     persona=t.get("persona")))
            )
        if repair_prompts:
            repair = _run_parallel(config, repair_prompts)
            for provider, model, _p, content in repair:
                ok = False
                report_payload: dict[str, Any] | None = None
                if content and not content.startswith("ERROR"):
                    try:
                        payload = _parse_report(content)
                        valid, _reason, report = validate_report(payload, gate, snapshot, mode, loose_evidence=True)
                        ok = valid
                        if valid:
                            report_payload = report  # type: ignore[assignment]
                            reports[f"{provider}:{model}"] = report  # type: ignore[assignment]
                    except Exception:
                        ok = False
                statements_1.append({
                    "provider": provider, "model": model,
                    "role": next(t["role"] for t in DEBATE_TEAM if t["model"] == model),
                    "content": content if not content.startswith("ERROR") else None,
                    "error": content if content.startswith("ERROR") else None,
                    "ok": ok,
                    "digest": _digest_report(report_payload),
                })
            # 修复轮结果并入第 1 轮展示（round 标 1.5 区分）
            rounds.append({"round": "1.5（修复）", "statements": statements_1[len(round1):]})

    # 第 2 轮：交叉辩论（看到另两家第 1 轮内容）
    if _aborted():
        return _aborted_result(rounds)
    round2_prompts = []
    for t in DEBATE_TEAM:
        provider, model = t["provider"], t["model"]
        others = [s for s in statements_1 if s["model"] != model]
        round2_prompts.append(
            (provider, model,
             build_debate_prompt(snapshot, gate, mode, t["role"], t["focus"],
                                 round_no=2, others=others))
        )
    round2 = _run_parallel(config, round2_prompts)
    statements_2 = [
        {
            "provider": provider, "model": model,
            "role": next(t["role"] for t in DEBATE_TEAM if t["model"] == model),
            "content": content if not content.startswith("ERROR") else None,
            "error": content if content.startswith("ERROR") else None,
            "ok": content is not None and not content.startswith("ERROR"),
        }
        for provider, model, _p, content in round2
    ]
    rounds.append({"round": 2, "statements": statements_2})

    # 第 3 轮（分歧大时自动）：以第 1 轮有效报告方向投票
    directions = [r.get("direction") for r in reports.values() if r.get("direction") in ("LONG", "SHORT", "NEUTRAL")]
    vote_long = directions.count("LONG")
    vote_short = directions.count("SHORT")
    has_disagreement = len(directions) >= 2 and not (vote_long >= 2 or vote_short >= 2)
    if has_disagreement and len(rounds) < max_rounds and not _aborted():
        round3_prompts = []
        for t in DEBATE_TEAM:
            provider, model = t["provider"], t["model"]
            round3_prompts.append(
                (provider, model,
                 build_debate_prompt(snapshot, gate, mode, t["role"], t["focus"],
                                     round_no=3, others=statements_2))
            )
        round3 = _run_parallel(config, round3_prompts)
        statements_3 = [
            {
                "provider": provider, "model": model,
                "role": next(t["role"] for t in DEBATE_TEAM if t["model"] == model),
                "content": content if not content.startswith("ERROR") else None,
                "error": content if content.startswith("ERROR") else None,
                "ok": content is not None and not content.startswith("ERROR"),
            }
            for provider, model, _p, content in round3
        ]
        rounds.append({"round": 3, "statements": statements_3})

    # 共识：方向投票 → 综合报告（票数多一方的报告为主，附分歧清单）
    valid_reports = list(reports.values())
    valid_count = len(valid_reports)
    consensus_report: dict[str, Any] | None = None
    if valid_count >= DEBATE_MIN_VALID:
        vote_dirs = [r.get("direction") for r in valid_reports]
        winner = max(set(vote_dirs), key=vote_dirs.count) if vote_dirs else "NEUTRAL"
        candidates = [r for r in valid_reports if r.get("direction") == winner]
        if candidates:
            consensus_report = dict(candidates[0])  # 主笔：票数多一方
            # 附分歧清单（其他方向）
            disagreements = [
                {"model": m, "direction": r.get("direction")}
                for m, r in reports.items() if r.get("direction") != winner
            ]
            consensus_report["debate_disagreements"] = disagreements
            consensus_report["debate_votes"] = {
                "LONG": vote_dirs.count("LONG"),
                "SHORT": vote_dirs.count("SHORT"),
                "NEUTRAL": vote_dirs.count("NEUTRAL"),
            }
    elif valid_count == 1:
        consensus_report = dict(valid_reports[0])
        consensus_report["debate_disagreements"] = [{"note": "仅一家有效报告，其余失败"}]
        consensus_report["debate_votes"] = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}

    return {
        "rounds": rounds,
        "consensus": {
            "direction": consensus_report.get("direction") if consensus_report else None,
            "report": consensus_report,
            "valid_count": valid_count,
        },
    }
