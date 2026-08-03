"""Aggregate review outcomes into the stats-card and context-breakdown payloads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .jobs import JobRecord

MEASUREMENT_DISCLAIMER = "测量层统计：样本小、未含滑点与费用、模型输出非平稳，不构成可实盘 edge 的证据。"
REVIEW_OUTCOMES = ("TP_FIRST", "SL_FIRST", "NOT_TRIGGERED", "EXPIRED_UNRESOLVED", "PENDING")


def compute_review_stats(records: list[JobRecord]) -> dict[str, Any]:
    """Aggregate review outcomes across jobs into the stats card payload."""
    counts: dict[str, int] = {
        "TP_FIRST": 0,
        "SL_FIRST": 0,
        "NOT_TRIGGERED": 0,
        "EXPIRED_UNRESOLVED": 0,
        "PENDING": 0,
    }
    r_values: list[float] = []
    reviewed = 0
    for record in records:
        review = record.review
        if not isinstance(review, dict):
            continue
        outcome = review.get("outcome")
        if outcome not in counts:
            continue
        counts[outcome] += 1
        reviewed += 1
        r_value = review.get("r_multiple")
        if outcome in ("TP_FIRST", "SL_FIRST") and isinstance(r_value, (int, float)):
            r_values.append(float(r_value))
    decided = counts["TP_FIRST"] + counts["SL_FIRST"]
    return {
        "reviewed": reviewed,
        "decided": decided,
        "win_rate": round(counts["TP_FIRST"] / decided, 3) if decided else None,
        "avg_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
        "counts": counts,
        "disclaimer": MEASUREMENT_DISCLAIMER,
    }


def _grouped_stats(
    records: list[JobRecord], key_fn: Callable[[JobRecord], str | None]
) -> dict[str, Any]:
    """按单一维度分组，每组复用整体统计口径（同样带免责声明）。"""
    buckets: dict[str, list[JobRecord]] = {}
    for record in records:
        review = record.review
        if not isinstance(review, dict) or review.get("outcome") not in REVIEW_OUTCOMES:
            continue
        key = key_fn(record)
        if key is None:
            continue
        buckets.setdefault(key, []).append(record)
    return {key: compute_review_stats(group) for key, group in sorted(buckets.items())}


def _gate_action_key(record: JobRecord) -> str | None:
    gate = record.gate
    return str(gate["action"]) if isinstance(gate, dict) and gate.get("action") else None


def _resonance_key(record: JobRecord) -> str | None:
    gate = record.gate
    if not isinstance(gate, dict):
        return None
    resonance = gate.get("resonance")
    if isinstance(resonance, dict) and resonance.get("available") is True:
        label = resonance.get("label")
        return label if isinstance(label, str) else None
    return None  # 共振不可用的任务不参与共振维度聚合


def _direction_key(record: JobRecord) -> str | None:
    report = record.report
    if isinstance(report, dict) and report.get("direction") in ("LONG", "SHORT"):
        return str(report["direction"])
    return None


def _prompt_version_key(record: JobRecord) -> str | None:
    gate = record.gate
    if isinstance(gate, dict) and isinstance(gate.get("prompt_version"), str):
        return gate["prompt_version"]
    return None


def _mode_key(record: JobRecord) -> str | None:
    """按交易模式分组（scalp / swing），实证哪种模式在何种情境下有 edge。"""
    mode = record.mode
    return mode if mode in ("scalp", "swing") else None


def _vol_regime_key(record: JobRecord) -> str | None:
    """按波动环境分组（iv_vs_hv 已随 gate_payload 落盘）。

    映射：iv_vs_hv=high → vol_high；low → vol_low；neutral → vol_neutral；
    IV 层不可用或字段缺失 → None（不参与该维度聚合）。
    """
    gate = record.gate
    if not isinstance(gate, dict):
        return None
    iv = gate.get("iv")
    if not isinstance(iv, dict):
        return None
    iv_vs_hv = iv.get("iv_vs_hv")
    if iv_vs_hv in ("high", "low", "neutral"):
        return f"vol_{iv_vs_hv}"
    return None


def _spread_percentile_key(record: JobRecord) -> str | None:
    """按点差历史分位分组（2026-08-03 新增）。

    依据：实盘复盘显示点差≥80分位的 33 单胜率仅 12%（-23.3R），
    点差正常组 46%（+9.9R）——点差是最大负期望来源，需持续监控闸门效果。
    映射：<0.5 → spread_low；0.5-0.8 → spread_mid；≥0.8 → spread_high。
    """
    gate = record.gate
    if not isinstance(gate, dict):
        return None
    tick = gate.get("tick_health")
    if not isinstance(tick, dict):
        return None
    percentile = tick.get("spread_percentile")
    if not isinstance(percentile, (int, float)):
        return None
    if percentile >= 0.8:
        return "spread_high"
    if percentile >= 0.5:
        return "spread_mid"
    return "spread_low"


def _session_key(record: JobRecord) -> str | None:
    """按交易时段分组（2026-08-03 新增）：监控时段纪律效果。

    快照 session_label 由 session_context 确定性计算；缺失 → None。
    """
    snapshot = record.snapshot
    if isinstance(snapshot, dict):
        label = snapshot.get("session_label")
        if isinstance(label, str) and label:
            return label
    return None


def compute_context_stats(records: list[JobRecord]) -> dict[str, Any]:
    """按交易员关心的单一维度切分复盘结果，回答"我的流程在什么情境下有 edge"。

    刻意只做单维度切分：样本量小，多维交叉会制造虚假规律（过拟合），
    那不是交易员该看的。每个分组复用整体统计口径并同样带免责声明。
    """
    return {
        "by_gate_action": _grouped_stats(records, _gate_action_key),
        "by_resonance": _grouped_stats(records, _resonance_key),
        "by_direction": _grouped_stats(records, _direction_key),
        "by_prompt_version": _grouped_stats(records, _prompt_version_key),
        "by_mode": _grouped_stats(records, _mode_key),
        "by_vol_regime": _grouped_stats(records, _vol_regime_key),
        "by_spread_percentile": _grouped_stats(records, _spread_percentile_key),
        "by_session": _grouped_stats(records, _session_key),
        "disclaimer": MEASUREMENT_DISCLAIMER,
    }
