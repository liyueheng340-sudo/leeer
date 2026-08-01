"""Deterministic gates for MT5 facts, event context and model eligibility."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal


GateAction = Literal["ANALYSE", "WATCH", "WAIT", "BLOCKED"]

# 点差峰值达到该值（价格单位）即视为异常扩大，ANALYSE 降级为 WATCH。
# 参考：XAUUSD 常态点差约 0.1-0.3，高影响事件前后可达 1.0-3.0。
SPREAD_DOWNGRADE_THRESHOLD = 0.8


@dataclass(frozen=True)
class GateResult:
    action: GateAction
    allow_model: bool
    directional_plan_allowed: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def tick_downgrade_reason(tick_health: dict[str, object] | None) -> str | None:
    """返回 tick 传感器触发的降级原因；传感器不可用时不做判断。"""
    if not isinstance(tick_health, dict) or tick_health.get("available") is not True:
        return None
    if tick_health.get("stalled") is True:
        detail = str(tick_health.get("stall_reason") or "").strip()
        suffix = f"（{detail}）" if detail else ""
        return f"报价流停滞{suffix}，降级为观察"
    spread_max = tick_health.get("spread_max")
    if isinstance(spread_max, (int, float)) and spread_max >= SPREAD_DOWNGRADE_THRESHOLD:
        return f"点差异常扩大（峰值 {spread_max:.2f}），降级为观察"
    return None


def ea_downgrade_reason(ea_status: dict[str, object] | None) -> tuple[str, str] | None:
    """返回 Cerberus EA 风控态触发的降级 (level, reason)；无需降级时返回 None。

    只消费风险机制字段（status / regime_blocked / hour_blocked），绝不读取
    持仓与盈亏——后者是事后测量，不构成预测证据（HY3 纪律）。映射只降级：
    PAUSED_NEWS → WAIT（禁模型，对齐事件窗口语义）；
    PAUSED_VOLATILITY / regime_blocked / hour_blocked → WATCH（技术面方向建议须带警示）。
    PAUSED_MANUAL / PAUSED_SCHEDULE 是操作选择而非市场证据，不降级。
    """
    if not isinstance(ea_status, dict) or ea_status.get("available") is not True:
        return None
    status = ea_status.get("status")
    if status == "PAUSED_NEWS":
        return ("WAIT", "EA 风控处于新闻事件窗口（Cerberus），禁模型分析")
    reasons: list[str] = []
    if status == "PAUSED_VOLATILITY":
        reasons.append("EA 风控触发波动率熔断")
    if ea_status.get("regime_blocked") is True:
        reasons.append("EA 报告 H1 强趋势机制（趋势否决生效）")
    if ea_status.get("hour_blocked") is True:
        reasons.append("EA 报告当前时段为高危波动窗口")
    if reasons:
        return ("WATCH", "；".join(reasons) + "，降级为观察")
    return None


def evaluate_gate(
    snapshot: dict[str, object],
    event_context: dict[str, object],
    now: datetime,
    tick_health: dict[str, object] | None = None,
    ea_status: dict[str, object] | None = None,
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
    ea_downgrade = ea_downgrade_reason(ea_status)
    # EA 新闻窗口与已核验事件窗口同级（都禁模型）；优先于"事件未核验"的 WATCH。
    if ea_downgrade is not None and ea_downgrade[0] == "WAIT":
        return GateResult("WAIT", False, False, ea_downgrade[1])
    if event_context.get("status") != "verified_clear":
        return GateResult("WATCH", True, True, "事件上下文未核验，允许技术面方向建议")
    # EA 波动率熔断/趋势否决/高危时段与 tick 传感器降级同级。
    if ea_downgrade is not None and ea_downgrade[0] == "WATCH":
        return GateResult("WATCH", True, True, ea_downgrade[1])
    downgrade = tick_downgrade_reason(tick_health)
    if downgrade is not None:
        return GateResult("WATCH", True, True, downgrade)
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
