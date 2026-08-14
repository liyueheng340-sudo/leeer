"""Aggregate review outcomes into the stats-card and context-breakdown payloads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .jobs import JobRecord
from .statistical import bonferroni_alpha, edge_decay, win_rate_with_ci

MEASUREMENT_DISCLAIMER = "测量层统计：样本小、未含滑点与费用、模型输出非平稳，不构成可实盘 edge 的证据。"
REVIEW_OUTCOMES = ("TP_FIRST", "SL_FIRST", "NOT_TRIGGERED", "EXPIRED_UNRESOLVED", "PENDING")
# 前向验证窗口：默认取最近 25 单已判定样本对比历史（满足统计最低样本门槛，
# 避免小窗口噪声。2026-08-07 新增）。
FORWARD_WINDOW = 25


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
    ci = win_rate_with_ci(counts["TP_FIRST"], decided) if decided else {
        "win_rate": None, "ci_low": None, "ci_high": None, "significant": None, "note": "无样本",
    }
    return {
        "reviewed": reviewed,
        "decided": decided,
        "win_rate": ci["win_rate"],
        "ci_low": ci["ci_low"],
        "ci_high": ci["ci_high"],
        "significant": ci["significant"],
        "note": ci["note"],
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
    统计验证层（2026-08-07，VARRD 式）：每个分组的 win_rate 带 Wilson 置信区间
    与显著性标记；因涉及多个维度同时比较，附 Bonferroni 校正后的显著性门槛——
    试的维度越多，单组"显著"越难（防 cherry-picking 挑好看的维度）。
    """
    dims = 8  # 下方分组维度数
    return {
        "by_gate_action": _grouped_stats(records, _gate_action_key),
        "by_resonance": _grouped_stats(records, _resonance_key),
        "by_direction": _grouped_stats(records, _direction_key),
        "by_prompt_version": _grouped_stats(records, _prompt_version_key),
        "by_mode": _grouped_stats(records, _mode_key),
        "by_vol_regime": _grouped_stats(records, _vol_regime_key),
        "by_spread_percentile": _grouped_stats(records, _spread_percentile_key),
        "by_session": _grouped_stats(records, _session_key),
        # Bonferroni：8 个维度同时比较，单组显著的 p 门槛降为 0.05/8=0.00625。
        "bonferroni_n": dims,
        "bonferroni_alpha": round(bonferroni_alpha(dims), 5),
        "note": "分组胜率带 Wilson 置信区间；多重维度比较已做 Bonferroni 校正，"
                "单组显著门槛 = 0.05/维度数。样本 <30 的组置信区间极宽，结论不可靠。",
        "disclaimer": MEASUREMENT_DISCLAIMER,
    }


def _decided_entries(records: list[JobRecord]) -> list[tuple[str, float]]:
    """提取已判定（TP_FIRST/SL_FIRST）样本的 (created_at, r_multiple)。

    TP_FIRST 用实际 r_multiple；SL_FIRST 计为 -1.0（满额亏损）。按创建时间升序。
    NOT_TRIGGERED / EXPIRED / PENDING 不参与期望计算（无 TP/SL 结果）。
    """
    entries: list[tuple[str, float]] = []
    for record in records:
        review = record.review
        if not isinstance(review, dict):
            continue
        outcome = review.get("outcome")
        if outcome == "TP_FIRST":
            r_value = review.get("r_multiple")
            if isinstance(r_value, (int, float)):
                entries.append((record.created_at, float(r_value)))
        elif outcome == "SL_FIRST":
            entries.append((record.created_at, -1.0))
    entries.sort(key=lambda item: item[0])
    return entries


def _window_summary(entries: list[tuple[str, float]]) -> dict[str, Any]:
    if not entries:
        return {"n": 0, "win_rate": None, "avg_r": None,
                "ci_low": None, "ci_high": None, "significant": None}
    wins = sum(1 for _, r in entries if r > 0)
    n = len(entries)
    ci = win_rate_with_ci(wins, n)
    return {
        "n": n,
        "win_rate": ci["win_rate"],
        "avg_r": round(sum(r for _, r in entries) / n, 3),
        "ci_low": ci["ci_low"],
        "ci_high": ci["ci_high"],
        "significant": ci["significant"],
    }


def compute_direction_quality(records: list[JobRecord]) -> dict[str, Any]:
    """方向×结果四分格（2026-08-07 P0）：区分"方向能力"与"执行点位能力"。

    同为 TP/SL 结果，加入 direction_correct（入场后价格是否曾向建议方向移动
    超过阈值）后分成四格：
      方向对 + TP  → 真 edge（方向对且执行对）
      方向对 + SL  → 点位差（方向看对但被扫损/止损太紧）← 隐藏问题
      方向错 + TP  → 运气（方向错但快速止盈）
      方向错 + SL  → 真失败（方向也错，执行也差）
    只统计已判定（TP/SL）且 direction_correct 已判定的样本。
    """
    cells = {
        "dir_correct_tp": [0, 0.0],
        "dir_correct_sl": [0, 0.0],
        "dir_wrong_tp": [0, 0.0],
        "dir_wrong_sl": [0, 0.0],
    }
    for record in records:
        review = record.review
        if not isinstance(review, dict):
            continue
        outcome = review.get("outcome")
        dc = review.get("direction_correct")
        if outcome not in ("TP_FIRST", "SL_FIRST") or not isinstance(dc, bool):
            continue
        r_value = review.get("r_multiple")
        r = float(r_value) if outcome == "TP_FIRST" and isinstance(r_value, (int, float)) else -1.0
        key = ("dir_correct" if dc else "dir_wrong") + ("_tp" if outcome == "TP_FIRST" else "_sl")
        cells[key][0] += 1
        cells[key][1] += r

    def _cell_stat(count: int, total_r: float) -> dict[str, Any]:
        if count == 0:
            return {"n": 0, "avg_r": None}
        return {"n": count, "avg_r": round(total_r / count, 3)}

    correct_total = cells["dir_correct_tp"][0] + cells["dir_correct_sl"][0]
    decided_total = correct_total + cells["dir_wrong_tp"][0] + cells["dir_wrong_sl"][0]
    rate = round(correct_total / decided_total, 3) if decided_total else None

    return {
        "dir_correct_tp": _cell_stat(*cells["dir_correct_tp"]),
        "dir_correct_sl": _cell_stat(*cells["dir_correct_sl"]),
        "dir_wrong_tp": _cell_stat(*cells["dir_wrong_tp"]),
        "dir_wrong_sl": _cell_stat(*cells["dir_wrong_sl"]),
        "direction_correct_rate": rate,
        "note": "方向×结果四分格：区分方向能力与执行点位能力。方向对+SL=点位差（隐藏痛点）；"
                "方向错+TP=运气。2026-08-07 P0。",
    }


def compute_forward_validation(
    records: list[JobRecord], recent_n: int = FORWARD_WINDOW
) -> dict[str, Any]:
    """前向验证：对比"最近 N 单"与"更早已判定单"的期望 R 与胜率。

    2026-08-07 新增：回答"新纪律/过滤规则生效后，edge 是否持续或改善"。
    只对比已判定样本，按创建时间切开为 recent（最近 N 单）与 earlier（更早）。
    不偷看数据：只用任务创建时间与事后 outcome，不依赖任何未来信息。
    当 recent_n 大于可判定样本总数时，recent 即全部、earlier 为空。
    """
    decided = _decided_entries(records)
    if not decided:
        return {
            "recent": _window_summary([]),
            "earlier": _window_summary([]),
            "recent_n": 0,
            "earlier_n": 0,
            "edge_decay": {"buckets": [], "decayed": False, "note": "暂无已判定样本"},
            "note": "暂无已判定样本，无法前向验证",
        }
    recent = decided[-recent_n:]
    earlier = decided[:-recent_n]
    # edge decay：按时间把全部已判定样本切成多段，看胜率是否随新近度衰减。
    all_ts = [ts for ts, _ in decided]
    all_out = ["TP_FIRST" if r > 0 else "SL_FIRST" for _, r in decided]
    decay = edge_decay(all_ts, all_out)
    return {
        "recent": _window_summary(recent),
        "earlier": _window_summary(earlier),
        "recent_n": len(recent),
        "earlier_n": len(earlier),
        "edge_decay": decay,
        "note": (
            f"前向验证：比较最近 {recent_n} 单与更早样本的期望 R。样本小、不含滑点费用，"
            "趋势变化需结合 review_stats 免责声明解读。edge_decay 追踪胜率是否随时间衰减。"
        ),
    }
