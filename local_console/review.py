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

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .brief import _parse_prices
from .config import ConsoleConfig
from .jobs import JobRecord, JobStore

ReviewOutcome = Literal[
    "TP_FIRST", "SL_FIRST", "NOT_TRIGGERED", "EXPIRED_UNRESOLVED", "PENDING"
]

REVIEW_WINDOW_HOURS = 24
REVIEW_TIMEFRAME = "M5"
REVIEW_BARS_TIMEOUT_SECONDS = 45
MEASUREMENT_DISCLAIMER = "测量层统计：样本小、未含滑点与费用、模型输出非平稳，不构成可实盘 edge 的证据。"
REVIEW_OUTCOMES = ("TP_FIRST", "SL_FIRST", "NOT_TRIGGERED", "EXPIRED_UNRESOLVED", "PENDING")


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
    """Walk bars in order and decide the outcome of a trade plan."""
    is_long = plan["direction"] == "LONG"
    entry_lo, entry_hi = plan["entry_lo"], plan["entry_hi"]
    tp, sl, mid = plan["take_profit"], plan["stop_loss"], plan["entry_mid"]
    risk = abs(mid - sl)
    reward = abs(tp - mid)
    window_end = min(now, created_at + timedelta(hours=REVIEW_WINDOW_HOURS))

    touched = False
    last_close: float | None = None
    for bar in bars:
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        last_close = close
        if not touched:
            if low <= entry_hi and high >= entry_lo:
                touched = True
            else:
                continue
        hit_tp = high >= tp if is_long else low <= tp
        hit_sl = low <= sl if is_long else high >= sl
        if hit_tp and hit_sl:
            # 同一根 K 线同时覆盖 TP 与 SL：顺序不可知，保守记为止损
            return _decided("SL_FIRST", -1.0, touched, bar, risk, reward)
        if hit_tp:
            r_multiple = round(reward / risk, 3) if risk > 0 else None
            return _decided("TP_FIRST", r_multiple, touched, bar, risk, reward)
        if hit_sl:
            return _decided("SL_FIRST", -1.0, touched, bar, risk, reward)

    if now < created_at + timedelta(hours=REVIEW_WINDOW_HOURS):
        return {
            "outcome": "PENDING",
            "entry_touched": touched,
            "r_multiple": None,
            "window_end": window_end.isoformat(),
        }
    if not touched:
        return {
            "outcome": "NOT_TRIGGERED",
            "entry_touched": False,
            "r_multiple": None,
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
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "entry_touched": touched,
        "r_multiple": r_multiple,
        "decided_bar_utc": datetime.fromtimestamp(int(bar["time"]), UTC).isoformat(),
        "risk_points": round(risk, 2),
        "reward_points": round(reward, 2),
    }


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


def compute_context_stats(records: list[JobRecord]) -> dict[str, Any]:
    """按交易员关心的单一维度切分复盘结果，回答“我的流程在什么情境下有 edge”。

    刻意只做单维度切分：样本量小，多维交叉会制造虚假规律（过拟合），
    那不是交易员该看的。每个分组复用整体统计口径并同样带免责声明。
    """
    return {
        "by_gate_action": _grouped_stats(records, _gate_action_key),
        "by_resonance": _grouped_stats(records, _resonance_key),
        "by_direction": _grouped_stats(records, _direction_key),
        "by_prompt_version": _grouped_stats(records, _prompt_version_key),
        "disclaimer": MEASUREMENT_DISCLAIMER,
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
        created = datetime.fromisoformat(record.created_at).astimezone(UTC)
        if created > now:
            continue
        due.append(record)
    return due


def fetch_review_bars(
    config: ConsoleConfig, start: datetime, end: datetime, tag: str = "review"
) -> list[dict[str, float]]:
    """Pull M5 bars for the review window via the read-only MT5 script."""
    if not config.mt5_python.is_file() or not config.review_script_path.is_file():
        return []
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    output = config.snapshots_dir / f"{tag}_bars.jsonl"
    command = [
        str(config.mt5_python),
        str(config.review_script_path),
        "--symbol",
        "XAUUSD",
        "--from-utc",
        start.isoformat(),
        "--to-utc",
        end.isoformat(),
        "--output",
        str(output),
    ]
    try:
        subprocess.run(
            command, check=True, capture_output=True, text=True,
            timeout=REVIEW_BARS_TIMEOUT_SECONDS,
        )
        bars: list[dict[str, float]] = []
        for line in output.read_text(encoding="utf-8").strip().splitlines():
            row = json.loads(line)
            if isinstance(row, dict) and {"time", "high", "low", "close"} <= set(row):
                bars.append(row)
        return bars
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def run_due_reviews(
    config: ConsoleConfig,
    store: JobStore,
    now: datetime | None = None,
    bars_runner=fetch_review_bars,
) -> int:
    """One MT5 call covers all due jobs; returns how many reviews were written."""
    reference_now = (now or datetime.now(UTC)).astimezone(UTC)
    records = store.list_recent(limit=200)
    due = due_for_review(records, reference_now)
    if not due:
        return 0
    earliest = min(datetime.fromisoformat(record.created_at).astimezone(UTC) for record in due)
    bars = bars_runner(config, earliest, reference_now)
    if not bars:
        return 0
    written = 0
    for record in due:
        plan = parse_trade_plan(record.report)
        created = datetime.fromisoformat(record.created_at).astimezone(UTC)
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
