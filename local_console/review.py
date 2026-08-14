"""Post-hoc quality review of console trade suggestions (measurement layer).

闭环定位：这是对"建议质量"的**测量**，不是交易能力证明。
每条带方向建议的已完成任务，在事后用 MT5 历史 M5 K 线判定：
- 入场区间是否被触及；
- 触及后止盈与止损谁先被命中（同一根 K 线同时命中时，保守记为止损）；
- 24 小时内两者都未命中 → 超时未决；入场区间从未触及 → 未触发。

统计输出（胜率、平均 R）会明确标注：样本小、未建模滑点与费用、
不构成任何可实盘 edge 的证据。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from .config import ConsoleConfig
from .jobs import JobRecord, JobStore
from .review_runs import (
    REVIEW_BARS_TIMEOUT_SECONDS,
    REVIEW_TIMEFRAME,
    fetch_review_bars,
)
from .review_stats import (
    MEASUREMENT_DISCLAIMER,
    REVIEW_OUTCOMES,
    compute_context_stats,
    compute_direction_quality,
    compute_forward_validation,
    compute_review_stats,
)
from .snapshot_facts import _parse_prices

ReviewOutcome = Literal[
    "TP_FIRST", "SL_FIRST", "NOT_TRIGGERED", "EXPIRED_UNRESOLVED", "PENDING"
]

REVIEW_WINDOW_HOURS = 24
# 方向对错判定阈值（2026-08-07 P0）：入场触及后，价格向建议方向移动达到
# 该比例 × 止损距离（risk）即视为"方向正确"。用于区分方向能力 vs 执行点位能力。
DIRECTION_CONFIRM_FRACTION = 0.5

__all__ = [
    "MEASUREMENT_DISCLAIMER",
    "REVIEW_BARS_TIMEOUT_SECONDS",
    "REVIEW_OUTCOMES",
    "REVIEW_TIMEFRAME",
    "REVIEW_WINDOW_HOURS",
    "ReviewOutcome",
    "compute_context_stats",
    "compute_direction_quality",
    "compute_forward_validation",
    "compute_review_stats",
    "due_for_review",
    "evaluate_plan",
    "evaluate_plan_with_costs",
    "fetch_review_bars",
    "parse_trade_plan",
    "run_due_reviews",
]


def parse_trade_plan(report: dict[str, object]) -> dict[str, Any] | None:
    """Extract a numeric trade plan from a stored report; None when absent/NEUTRAL."""
    direction = report.get("direction")
    if direction not in ("LONG", "SHORT"):
        return None
    entry = _parse_prices(report.get("entry_zone"))
    take_profit = _parse_prices(report.get("take_profit"))
    stop_loss = _parse_prices(report.get("stop_loss"))
    if not entry or len(take_profit) != 1 or len(stop_loss) != 1:
        return None
    entry_lo, entry_hi = min(entry), max(entry)
    entry_mid = (entry_lo + entry_hi) / 2
    return {
        "direction": direction,
        "entry_lo": entry_lo,
        "entry_hi": entry_hi,
        "entry_mid": entry_mid,
        "take_profit": take_profit[0],
        "stop_loss": stop_loss[0],
    }


def evaluate_plan(
    plan: dict[str, Any],
    bars: Iterable[dict[str, float]],
    created_at: datetime,
    now: datetime,
) -> dict[str, Any]:
    """Walk bars in order and decide the outcome of a trade plan.

    2026-08-07 P0 增强：除 TP/SL 结果外，判定"方向对错"（direction_correct）。
    区分方向能力与执行/点位能力——同为 SL_FIRST，方向对是点位差、方向错是真失败。
    方向判定：入场触及后，价格向建议方向的最大有利偏移 ≥ DIRECTION_CONFIRM_FRACTION × risk
    即视为方向正确（risk = 入场中部到止损的距离，作 ATR-like 尺度）。
    """
    is_long = plan["direction"] == "LONG"
    entry_lo, entry_hi = plan["entry_lo"], plan["entry_hi"]
    tp, sl, mid = plan["take_profit"], plan["stop_loss"], plan["entry_mid"]
    risk = abs(mid - sl)
    reward = abs(tp - mid)
    window_end = min(now, created_at + timedelta(hours=REVIEW_WINDOW_HOURS))
    confirm_threshold = risk * DIRECTION_CONFIRM_FRACTION

    touched = False
    last_close: float | None = None
    max_favorable: float = 0.0  # 入场触及后向建议方向的最大偏移（价格单位）
    for bar in bars:
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        last_close = close
        if not touched:
            if low <= entry_hi and high >= entry_lo:
                touched = True
            else:
                continue
        # 追踪方向有利偏移（入场触及后才有意义）
        favorable = max(high - mid, 0.0) if is_long else max(mid - low, 0.0)
        if favorable > max_favorable:
            max_favorable = favorable
        hit_tp = high >= tp if is_long else low <= tp
        hit_sl = low <= sl if is_long else high >= sl
        if hit_tp and hit_sl:
            # 同一根 K 线同时覆盖 TP 与 SL：顺序不可知，保守记为止损
            return _decided("SL_FIRST", -1.0, touched, bar, risk, reward,
                            direction_correct=max_favorable >= confirm_threshold)
        if hit_tp:
            r_multiple = round(reward / risk, 3) if risk > 0 else None
            return _decided("TP_FIRST", r_multiple, touched, bar, risk, reward,
                            direction_correct=max_favorable >= confirm_threshold)
        if hit_sl:
            return _decided("SL_FIRST", -1.0, touched, bar, risk, reward,
                            direction_correct=max_favorable >= confirm_threshold)

    if now < created_at + timedelta(hours=REVIEW_WINDOW_HOURS):
        return {
            "outcome": "PENDING",
            "entry_touched": touched,
            "r_multiple": None,
            "direction_correct": None,
            "window_end": window_end.isoformat(),
        }
    if not touched:
        return {
            "outcome": "NOT_TRIGGERED",
            "entry_touched": False,
            "r_multiple": None,
            "direction_correct": None,
            "window_end": window_end.isoformat(),
        }
    floating = None
    if last_close is not None:
        sign = 1 if is_long else -1
        floating = round((last_close - mid) * sign, 2)
    return {
        "outcome": "EXPIRED_UNRESOLVED",
        "entry_touched": True,
        "r_multiple": None,
        "direction_correct": max_favorable >= confirm_threshold,
        "floating_points": floating,
        "window_end": window_end.isoformat(),
    }


def _decided(
    outcome: ReviewOutcome,
    r_multiple: float | None,
    touched: bool,
    bar: dict[str, float],
    risk: float,
    reward: float,
    direction_correct: bool | None = None,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "entry_touched": touched,
        "r_multiple": r_multiple,
        "direction_correct": direction_correct,
        "decided_bar_utc": datetime.fromtimestamp(int(bar["time"]), timezone.utc).isoformat(),
        "risk_points": round(risk, 2),
        "reward_points": round(reward, 2),
    }


def due_for_review(records: list[JobRecord], now: datetime) -> list[JobRecord]:
    """COMPLETED jobs with a directional plan whose review is missing or PENDING."""
    due: list[JobRecord] = []
    for record in records:
        if record.stage != "COMPLETE" or not isinstance(record.report, dict):
            continue
        plan = parse_trade_plan(record.report)
        if plan is None:
            continue
        review = record.review
        if isinstance(review, dict) and review.get("outcome") not in (None, "PENDING"):
            continue  # 已有终态结论
        created = datetime.fromisoformat(record.created_at).astimezone(timezone.utc)
        if created > now:
            continue
        due.append(record)
    return due


def run_due_reviews(
    config: ConsoleConfig,
    store: JobStore,
    now: datetime | None = None,
    bars_runner=fetch_review_bars,
) -> int:
    """One MT5 call covers all due jobs; returns how many reviews were written."""
    reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    records = store.list_recent(limit=200)
    due = due_for_review(records, reference_now)
    if not due:
        return 0
    earliest = min(datetime.fromisoformat(record.created_at).astimezone(timezone.utc) for record in due)
    bars = bars_runner(config, earliest, reference_now)
    if not bars:
        return 0
    written = 0
    for record in due:
        plan = parse_trade_plan(record.report)
        created = datetime.fromisoformat(record.created_at).astimezone(timezone.utc)
        window_end = min(reference_now, created + timedelta(hours=REVIEW_WINDOW_HOURS))
        window_bars = [
            bar
            for bar in bars
            if int(created.timestamp()) <= int(bar["time"]) <= int(window_end.timestamp())
        ]
        review = evaluate_plan(plan, window_bars, created, reference_now)
        review["reviewed_at"] = reference_now.isoformat()
        store.set_review(record.id, review)
        written += 1
    return written


def evaluate_plan_with_costs(
    plan: dict[str, Any],
    bars: Iterable[dict[str, float]],
    created_at: datetime,
    now: datetime,
    spread: float = 0.0,
) -> dict[str, Any]:
    """回测专用判定：计入点差/滑点成本 + Intra-bar Path Bias 改进（不改实盘 evaluate_plan）。

    2026-08-07 回测可信性增强（采纳三方批判）：
    1. Intra-bar 改进：同一根 K 线同时触及 TP 和 SL 时，不"一律计止损"——按
       "开盘价到 TP 距离 : 开盘价到 SL 距离"的比例判定谁更可能先触及。M15 高波动下
       "一律计止损"会高估止损（技术派批判的 Intra-bar Path Bias）。
       ratio = dist_to_tp / (dist_to_tp + dist_to_sl)；随机游走下 TP 先触及概率≈ratio。
    2. 点差/滑点成本：出入场各扣 spread/2（买卖两次），折成 R 计入 r_multiple。
    返回结构与 evaluate_plan 一致 + 额外 cost_r / intra_bar 字段。
    """
    is_long = plan["direction"] == "LONG"
    entry_lo, entry_hi = plan["entry_lo"], plan["entry_hi"]
    tp, sl, mid = plan["take_profit"], plan["stop_loss"], plan["entry_mid"]
    risk = abs(mid - sl)
    reward = abs(tp - mid)
    window_end = min(now, created_at + timedelta(hours=REVIEW_WINDOW_HOURS))
    confirm_threshold = risk * DIRECTION_CONFIRM_FRACTION

    touched = False
    last_close: float | None = None
    max_favorable = 0.0
    for bar in bars:
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        last_close = close
        if not touched:
            if low <= entry_hi and high >= entry_lo:
                touched = True
            else:
                continue
        favorable = max(high - mid, 0.0) if is_long else max(mid - low, 0.0)
        if favorable > max_favorable:
            max_favorable = favorable
        hit_tp = high >= tp if is_long else low <= tp
        hit_sl = low <= sl if is_long else high >= sl
        if hit_tp and hit_sl:
            # Intra-bar 同时触及：谁近先触及谁（不一律计止损）。
            # close 离 TP 更近 → TP 先触及；离 SL 更近 → SL 先触及。
            dist_tp = abs(close - tp)
            dist_sl = abs(close - sl)
            ratio = dist_sl / (dist_tp + dist_sl) if (dist_tp + dist_sl) > 0 else 0.5
            if ratio >= 0.5:  # 离 SL 更远 = 离 TP 更近 → 判 TP
                r = round(reward / risk, 3) if risk > 0 else None
                out, rr = "TP_FIRST", r
            else:  # 离 SL 更近 → 判 SL
                out, rr = "SL_FIRST", -1.0
            result = _decided(out, rr, touched, bar, risk, reward,
                              direction_correct=max_favorable >= confirm_threshold)
            result["intra_bar"] = round(ratio, 3)
            return _apply_cost(result, spread)
        if hit_tp:
            r = round(reward / risk, 3) if risk > 0 else None
            return _apply_cost(
                _decided("TP_FIRST", r, touched, bar, risk, reward,
                         direction_correct=max_favorable >= confirm_threshold), spread)
        if hit_sl:
            return _apply_cost(
                _decided("SL_FIRST", -1.0, touched, bar, risk, reward,
                         direction_correct=max_favorable >= confirm_threshold), spread)

    if now < created_at + timedelta(hours=REVIEW_WINDOW_HOURS):
        return {"outcome": "PENDING", "entry_touched": touched, "r_multiple": None,
                "direction_correct": None, "window_end": window_end.isoformat()}
    if not touched:
        return {"outcome": "NOT_TRIGGERED", "entry_touched": False, "r_multiple": None,
                "direction_correct": None, "window_end": window_end.isoformat()}
    floating = None
    if last_close is not None:
        sign = 1 if is_long else -1
        floating = round((last_close - mid) * sign, 2)
    return {"outcome": "EXPIRED_UNRESOLVED", "entry_touched": True, "r_multiple": None,
            "direction_correct": max_favorable >= confirm_threshold,
            "floating_points": floating, "window_end": window_end.isoformat()}


def _apply_cost(result: dict[str, Any], spread: float) -> dict[str, Any]:
    """把点差/滑点成本折成 R 扣进 r_multiple（出入场各 spread/2，买卖两次）。"""
    if result.get("r_multiple") is None:
        return result
    risk = result.get("risk_points")
    if isinstance(risk, (int, float)) and risk > 0 and spread > 0:
        cost_r = spread / risk
        result["r_multiple"] = round(float(result["r_multiple"]) - cost_r, 3)
        result["cost_r"] = round(cost_r, 4)
    return result
