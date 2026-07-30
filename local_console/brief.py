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
ALLOWED_ACTIONS = {"ANALYSE", "WATCH", "WAIT"}
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
    if not config.backend_url:
        raise RuntimeError("Qwen backend URL is not configured")
    llm = create_llm_client("qwen", model, config.backend_url).get_llm()
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
        "task_kind": kind,
        "gate_action": gate.action,
        "directional_plan_allowed": gate.directional_plan_allowed,
        "allowed_sources": allowed_sources,
        "facts": snapshot,
        "required_json_keys": sorted(REQUIRED_KEYS),
        "output_rules": [
            "Return one JSON object and no markdown.",
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
        return False, "report is not a JSON object", None
    missing = REQUIRED_KEYS - set(payload)
    if missing:
        return False, f"report is missing required fields: {', '.join(sorted(missing))}", None
    if payload.get("action") not in ALLOWED_ACTIONS:
        return False, "report action is invalid", None
    source_ids = payload.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids or not all(
        isinstance(source, str) for source in source_ids
    ):
        return False, "report source_ids are invalid", None
    allowed_sources = {"mt5_snapshot"}
    if gate.action == "ANALYSE":
        allowed_sources.add("verified_event_context")
    for source in source_ids:
        if source not in allowed_sources:
            return False, f"report cites an unprovided source: {source}", None
    for key in REQUIRED_KEYS - {"source_ids"}:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            return False, f"report field is invalid: {key}", None
    visible_text = " ".join(
        str(payload[key]) for key in ("summary", "invalidation", "next_observation")
    )
    if not gate.directional_plan_allowed and DIRECT_ENTRY_PATTERN.search(visible_text):
        return False, "WATCH report contains a direct entry instruction", None
    return True, "report accepted", dict(payload)
