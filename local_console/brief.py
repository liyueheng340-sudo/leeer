"""Constrained Qwen research with a deterministic report acceptance gate."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from tradingagents.llm_clients.factory import create_llm_client

from .config import ConsoleConfig
from .guard import GateResult
from .jobs import JobKind
from .prompt_rules import (
    ALLOWED_DIRECTIONS,
    ALLOWED_LATIN_TERMS,
    PROMPT_VERSION,
    REQUIRED_KEYS,
    TRADE_KEYS,
    VISIBLE_TEXT_KEYS,
    allowed_source_ids,
    build_prompt,
)
from .report_validation import validate_report

# 单次瞬时失败（网络抖动 / 偶发坏 JSON）不应让整个任务失败：
# 在应用层做有限重试（客户端层 max_retries 保持 0，避免双层重试叠加）。
MODEL_MAX_RETRIES = 1
MODEL_RETRY_BACKOFF_SECONDS = 5
# 快评也用推理模型（qwen3.7-max 实测常态 73s、偶发 90-100s）：90s 上限会在
# 临界点频繁超时触发重试（总耗时翻倍到 185s）。放宽到 120s 留足余量。
MODEL_TIMEOUT_SECONDS = 120
# 深度复盘用的是推理模型，显著比快评的轻量模型慢（实测常态 55-90 秒、偶尔更长）：
# 若沿用快评的 90 秒上限，会在临界点频繁超时失败，故单独放宽到 180 秒。
DEEP_MODEL_TIMEOUT_SECONDS = 180
# 深度复盘超时几乎总是"模型持续偏慢"而非瞬时抖动，重试只会让唯一 worker 再被独占
# 三分钟、阻塞期间所有快评与自主调度，故深度复盘不做应用层重试（快评保留重试）。
DEEP_MODEL_MAX_RETRIES = 0

__all__ = [
    "ALLOWED_DIRECTIONS",
    "ALLOWED_LATIN_TERMS",
    "DEEP_MODEL_MAX_RETRIES",
    "DEEP_MODEL_TIMEOUT_SECONDS",
    "MODEL_MAX_RETRIES",
    "MODEL_RETRY_BACKOFF_SECONDS",
    "MODEL_TIMEOUT_SECONDS",
    "PROMPT_VERSION",
    "REQUIRED_KEYS",
    "TRADE_KEYS",
    "VISIBLE_TEXT_KEYS",
    "_invoke_with_retry",
    "_parse_model_json",
    "allowed_source_ids",
    "build_prompt",
    "request_brief",
    "validate_report",
    "worst_case_seconds",
]


def request_brief(
    config: ConsoleConfig,
    kind: JobKind,
    snapshot: dict[str, object],
    gate: GateResult,
    mode: str = "scalp",
) -> object:
    is_deep = kind == "deep_review"
    model = config.deep_model if is_deep else config.quick_model
    timeout = DEEP_MODEL_TIMEOUT_SECONDS if is_deep else MODEL_TIMEOUT_SECONDS
    max_retries = DEEP_MODEL_MAX_RETRIES if is_deep else MODEL_MAX_RETRIES
    prompt = build_prompt(snapshot, gate, kind, mode)
    try:
        llm = create_llm_client(
            "qwen", model, config.backend_url, timeout=timeout, max_retries=0
        ).get_llm()
        return _invoke_with_retry(llm.invoke, prompt, max_retries)
    except Exception as primary_error:
        # 双 key 冗余（2026-08-03）：主端点失败/额度耗尽时切到备用端点重试一次。
        # 阿里云双区独立计费——主套餐耗尽备用仍可用，避免"简报要点几次才出来"。
        if config.fallback_backend_url and config.fallback_api_key:
            try:
                fallback_llm = create_llm_client(
                    "openai_compatible", model, config.fallback_backend_url,
                    api_key=config.fallback_api_key, timeout=timeout, max_retries=0,
                ).get_llm()
                return _invoke_with_retry(fallback_llm.invoke, prompt, max_retries)
            except Exception:
                pass  # 备用也失败：抛原始主端点错误，便于诊断
        raise RuntimeError("Qwen 分析多次重试仍失败") from primary_error


def _parse_model_json(content: str) -> object:
    """解析模型输出的 JSON，容忍 markdown 围栏与前后夹带文字。

    提示词要求裸 JSON，但模型偶发用 ```json 围栏或夹带短句；宽松解析把这类
    "形式瑕疵"从任务失败转为可用，内容契约仍由 validate_report 严格把关。
    解析候选依次：原文 → 围栏内文本 → 首个 { 至末个 } 的子串。
    """
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
    raise RuntimeError("Qwen response is not JSON") from last_error


def _invoke_with_retry(
    invoke: Any, prompt: str, max_retries: int = MODEL_MAX_RETRIES
) -> object:
    """调用模型并解析 JSON，瞬时失败时退避重试有限次。

    抽出为纯函数（接受任意 invoke 可调用对象）以便无需真实 LLM 即可测试重试逻辑。
    max_retries 按任务类型传入：快评容忍瞬时抖动可重试，深度复盘超时多为持续偏慢不重试。
    """
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        if attempt:
            time.sleep(MODEL_RETRY_BACKOFF_SECONDS)
        try:
            response = invoke(prompt)
            content = getattr(response, "content", response)
            if not isinstance(content, str):
                raise RuntimeError("Qwen response is not text")
            return _parse_model_json(content)
        except Exception as error:  # 网络/超时/偶发坏 JSON 等瞬时故障
            last_error = error
    raise RuntimeError("Qwen 分析多次重试仍失败") from last_error


def worst_case_seconds(kind: JobKind) -> float:
    """模型阶段最坏耗时（每次超时 × 尝试次数 + 重试退避）。

    供陈旧任务阈值取两种任务的最大值，确保深度复盘的长推理不会被误判为超时。
    深度复盘已是三家辩论（第 1 轮并行 + 失败修复轮 + 第 2 轮交叉 + 分歧时第 3 轮）：
    每轮最坏 DEBATE_TIMEOUT_SECONDS，**最坏路径含修复轮共 4 轮调用**——
    若只按 3 轮算（720s），陈旧扫描会在辩论中途误杀任务（2026-08-03 实测：
    04:43 单辩论 752 秒被 FAILED，恰好撞上 750s 阈值，任务并未失败）。
    2026-08-04 审查修复（INCR）：单轮内每家还有 DEBATE_RETRIES 次重试
    （240 + 2s 退避 + 240），真实单轮最坏 482s——补算重试，避免心跳失效时
    陈旧阈值低估（真实最坏 ~1928s > 990s 阈值）。
    """
    if kind == "deep_review":
        from .debate import DEBATE_MAX_ROUNDS, DEBATE_RETRIES, DEBATE_TIMEOUT_SECONDS

        # 修复轮 + 最大轮数 = 至多 4 轮调用；每轮每家最多 (RETRIES+1) 次调用。
        per_round = DEBATE_TIMEOUT_SECONDS * (DEBATE_RETRIES + 1) + 2 * DEBATE_RETRIES
        return per_round * (DEBATE_MAX_ROUNDS + 1)
    timeout, retries = MODEL_TIMEOUT_SECONDS, MODEL_MAX_RETRIES
    return timeout * (retries + 1) + MODEL_RETRY_BACKOFF_SECONDS * retries
