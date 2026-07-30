"""Constrained Qwen research with a deterministic report acceptance gate."""

from __future__ import annotations

import json
import re
from typing import Any

from tradingagents.llm_clients.factory import create_llm_client

from .config import ConsoleConfig
from .guard import GateResult
from .jobs import JobKind

REQUIRED_KEYS = {"action", "source_ids", "summary", "invalidation", "next_observation"}
VISIBLE_TEXT_KEYS = ("summary", "invalidation", "next_observation")
MODEL_TIMEOUT_SECONDS = 90
ALLOWED_ACTIONS = {"ANALYSE", "WATCH", "WAIT"}
ALLOWED_LATIN_TERMS = {
    "ASK",
    "ATR",
    "BID",
    "BITGET",
    "H1",
    "H4",
    "M1",
    "M5",
    "M15",
    "MT5",
    "QWEN",
    "SL",
    "TP",
    "WATCH",
    "XAUUSD",
}
DIRECT_ENTRY_PATTERN = re.compile(
    r"\b(buy|sell|long|short)\s+(now|immediately)\b|立即买入|立即卖出|立即开多|立即开空|马上买入|马上卖出",
    re.IGNORECASE,
)


def request_brief(
    config: ConsoleConfig,
    kind: JobKind,
    snapshot: dict[str, object],
    gate: GateResult,
) -> object:
    model = config.deep_model if kind == "deep_review" else config.quick_model
    llm = create_llm_client(
        "qwen", model, config.backend_url, timeout=MODEL_TIMEOUT_SECONDS, max_retries=0
    ).get_llm()
    response = llm.invoke(build_prompt(snapshot, gate, kind))
    content = getattr(response, "content", response)
    if not isinstance(content, str):
        raise RuntimeError("Qwen response is not text")
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise RuntimeError("Qwen response is not valid JSON") from error


def build_prompt(snapshot: dict[str, object], gate: GateResult, kind: JobKind) -> str:
    allowed_sources = ["mt5_snapshot"]
    if gate.action == "ANALYSE":
        allowed_sources.append("verified_event_context")
    contract = {
        "role": "XAU manual analysis assistant",
        "output_language": "Simplified Chinese",
        "task_kind": kind,
        "gate_action": gate.action,
        "directional_plan_allowed": gate.directional_plan_allowed,
        "allowed_sources": allowed_sources,
        "facts": snapshot,
        "required_json_keys": sorted(REQUIRED_KEYS),
        "output_rules": [
            "Return one JSON object and no markdown.",
            "所有 summary、invalidation、next_observation 字段必须使用简体中文；不得用英文输出。",
            "Use only allowed_sources.",
            "Do not claim an unprovided price, indicator, news item, or event.",
            "Do not promise returns or describe automated execution.",
            "When directional_plan_allowed is false, provide observation and wait conditions only.",
        ],
    }
    return json.dumps(contract, ensure_ascii=False)


def validate_report(
    payload: object, gate: GateResult
) -> tuple[bool, str, dict[str, object] | None]:
    if not isinstance(payload, dict):
        return False, "报告不是 JSON 对象", None
    missing = REQUIRED_KEYS - set(payload)
    if missing:
        return False, f"报告缺少字段：{', '.join(sorted(missing))}", None
    action = payload.get("action")
    if action not in ALLOWED_ACTIONS:
        return False, "报告动作无效", None
    if gate.action == "WATCH" and action != "WATCH":
        return False, "观察模式报告动作必须是 WATCH", None
    if gate.action == "WAIT" and action != "WAIT":
        return False, "等待模式报告动作必须是 WAIT", None
    source_ids = payload.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids or not all(
        isinstance(source, str) for source in source_ids
    ):
        return False, "报告来源标识无效", None
    allowed_sources = {"mt5_snapshot"}
    if gate.action == "ANALYSE":
        allowed_sources.add("verified_event_context")
    for source in source_ids:
        if source not in allowed_sources:
            return False, f"报告引用了未提供的数据源：{source}", None
    for key in VISIBLE_TEXT_KEYS:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            return False, f"报告字段无效：{key}", None
        latin_terms = re.findall(r"\b[A-Za-z][A-Za-z0-9]*\b", payload[key])
        if not re.search(r"[\u4e00-\u9fff]", payload[key]) or any(
            term.upper() not in ALLOWED_LATIN_TERMS for term in latin_terms
        ):
            return False, f"报告正文必须使用中文：{key}", None
    visible_text = " ".join(
        str(payload[key]) for key in ("summary", "invalidation", "next_observation")
    )
    if not gate.directional_plan_allowed and DIRECT_ENTRY_PATTERN.search(visible_text):
        return False, "观察模式报告包含直接入场指令", None
    return True, "报告已验收", dict(payload)
