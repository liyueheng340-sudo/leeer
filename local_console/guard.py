"""Deterministic gates for MT5 facts, event context and model eligibility."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal


GateAction = Literal["ANALYSE", "WATCH", "WAIT", "BLOCKED"]


@dataclass(frozen=True)
class GateResult:
    action: GateAction
    allow_model: bool
    directional_plan_allowed: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_gate(
    snapshot: dict[str, object], event_context: dict[str, object], now: datetime
) -> GateResult:
    if snapshot.get("identity_match") is not True or snapshot.get("symbol") != "XAUUSD":
        return GateResult("BLOCKED", False, False, "MT5 经纪商或品种身份不匹配")
    if not valid_quote(snapshot):
        return GateResult("BLOCKED", False, False, "MT5 报价不可用")
    if snapshot_age_seconds(snapshot, now) > 60:
        return GateResult("BLOCKED", False, False, "MT5 快照已超过 60 秒")
    if event_context.get("status") == "wait":
        reason = str(event_context.get("reason") or "已核验的高影响事件窗口")
        return GateResult("WAIT", False, False, reason)
    if event_context.get("status") != "verified_clear":
        return GateResult("WATCH", True, False, "事件上下文未核验")
    return GateResult("ANALYSE", True, True, "MT5 快照新鲜且事件状态已核验")


def valid_quote(snapshot: dict[str, object]) -> bool:
    bid, ask = snapshot.get("bid"), snapshot.get("ask")
    return (
        isinstance(bid, (int, float))
        and isinstance(ask, (int, float))
        and bid > 0
        and ask > 0
        and ask >= bid
    )


def snapshot_age_seconds(snapshot: dict[str, object], now: datetime) -> float:
    timestamp = snapshot.get("timestamp")
    if not isinstance(timestamp, str):
        return float("inf")
    try:
        captured = datetime.fromisoformat(timestamp).astimezone(UTC)
    except ValueError:
        return float("inf")
    return max(0.0, (now.astimezone(UTC) - captured).total_seconds())


def load_event_context(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"status": "unverified"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unverified"}
    if not isinstance(payload, dict) or payload.get("status") not in {"verified_clear", "wait"}:
        return {"status": "unverified"}
    return payload
