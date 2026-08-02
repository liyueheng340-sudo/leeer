"""Acceptance validation of model reports against the gate and snapshot facts.

军师模式：方向/点位纪律（共振相悖、震荡市强方向、RSI 极端追单、
入场不贴关键价位）由硬拒绝改为风险标注，随报告呈现给交易者。
真实性与几何校验（来源白名单、中文正文、价格几何、幅度上限、证据字段）保留硬约束。
"""

from __future__ import annotations

import re

from .guard import GateResult
from .jobs import JobMode
from .prompt_rules import (
    ALLOWED_DIRECTIONS,
    ALLOWED_LATIN_TERMS,
    MODE_RULES,
    REQUIRED_KEYS,
    SUGGESTION_KEYS,
    TRADE_KEYS,
    VISIBLE_TEXT_KEYS,
    _clean_warning,
    allowed_source_ids,
)
from .snapshot_facts import _key_levels, _parse_prices, _reference_atr

EVIDENCE_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9_.]+$")
MAX_EVIDENCE_FIELDS = 12


def _mode_limits(mode: str) -> tuple[float, float, float, float]:
    """按交易模式取入场容差 / 入场偏离报价 / TP / SL 上限（与 prompt MODE_RULES 同表）。"""
    rules = MODE_RULES.get(mode, MODE_RULES["scalp"])
    entry_tol = float(rules["entry_atr_tolerance"])
    tp_limit = float(rules["tp_atr_limit"])
    sl_limit = float(rules["sl_atr_limit"])
    # 入场偏离快照报价的上限：波段模式允许更宽的容差（结构入场），按 ATR 倍数同比放宽。
    bid_tol = 3.0 if mode == "scalp" else 4.5
    return entry_tol, bid_tol, tp_limit, sl_limit


def _resolve_evidence_path(snapshot: dict[str, object], path: str) -> bool:
    """True when a dotted evidence path ('timeframe_structure.h1.atr_14') exists in facts."""
    from typing import Any

    current: Any = snapshot
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return current is not None


def _validate_resonance_direction(
    payload: dict[str, object], snapshot: dict[str, object]
) -> str | None:
    """强方向建议与共振相悖时给出风险标注（军师模式：不拒绝，只标注）。

    实测复盘：SHORT（顺势）30 样本 avg R +0.40，LONG（逆势）7 样本 -0.28。
    共振由已收盘 K 线确定性计算，非模型推断，可作为验收风险标注。
    共振不可用或 |score|<0.5 时不做此校验（不明确 = 无强制方向）。
    """
    direction = payload.get("direction")
    if direction not in ("LONG", "SHORT"):
        return None
    resonance = snapshot.get("timeframe_resonance")
    if not isinstance(resonance, dict) or resonance.get("available") is not True:
        return None
    score = resonance.get("score")
    if not isinstance(score, (int, float)) or abs(score) < 0.5:
        return None
    if direction == "LONG" and score < 0:
        return "方向与多周期共振相悖：共振偏空时做多胜率偏低"
    if direction == "SHORT" and score > 0:
        return "方向与多周期共振相悖：共振偏多时做空胜率偏低"
    return None


def _validate_regime_direction(
    payload: dict[str, object], snapshot: dict[str, object]
) -> str | None:
    """强方向建议与市场状态相悖时给出风险标注（军师模式：不拒绝，只标注）。

    market_regime 由已收盘 K 线 ADX/RSI/StdDev 确定性计算（EA 精华参数工程），
    非模型推断，可作为验收风险标注：震荡市（双周期 ADX<20）追单胜率天然偏低；
    强趋势市逆势开仓（空头结构做多）与共振校验同级标注。
    状态不可用或 direction 为 NEUTRAL 时跳过。
    """
    direction = payload.get("direction")
    if direction not in ("LONG", "SHORT"):
        return None
    regime = snapshot.get("market_regime")
    if not isinstance(regime, dict) or regime.get("available") is not True:
        return None
    if regime.get("regime") == "ranging":
        return "市场处于震荡市（双周期 ADX < 20），强方向建议胜率偏低"
    if regime.get("regime") == "trending":
        trend_direction = regime.get("trend_direction")
        if trend_direction == "buy" and direction == "SHORT":
            return "强趋势市偏多（双周期 ADX ≥ 25），逆势做空胜率偏低"
        if trend_direction == "sell" and direction == "LONG":
            return "强趋势市偏空（双周期 ADX ≥ 25），逆势做多胜率偏低"
    extreme = regime.get("rsi_extreme")
    if isinstance(extreme, dict):
        if extreme.get("side") == "overbought" and direction == "LONG":
            return "RSI 超买，追多胜率偏低"
        if extreme.get("side") == "oversold" and direction == "SHORT":
            return "RSI 超卖，追空胜率偏低"
    return None


def _validate_entry_at_key_level(
    payload: dict[str, object], snapshot: dict[str, object], mode: JobMode = "scalp"
) -> str | None:
    """强方向建议的入场必须贴近关键价位（支撑买/阻力卖是点位纪律）。

    容差按模式取：scalp 贴关键价位（±1.0 ATR），swing 允许结构入场（±2.5 ATR）。
    无关键价位或方向 NEUTRAL 时跳过；缺失 ATR 基准时放宽为最近整数关口 ±2.0。
    """
    if payload.get("direction") == "NEUTRAL":
        return None
    entry = _parse_prices(payload.get("entry_zone"))
    if not entry:
        return None  # 几何校验已报告解析失败
    levels = _key_levels(snapshot)
    if not levels:
        return None
    atr = _reference_atr(snapshot)
    entry_tol, _bid_tol, _tp, _sl = _mode_limits(mode)
    tolerance = entry_tol * atr if isinstance(atr, (int, float)) and atr > 0 else 2.0
    entry_mid = (min(entry) + max(entry)) / 2
    if any(abs(entry_mid - level) <= tolerance for level in levels):
        return None
    return "入场区间未贴近任何关键价位（前日高低/当日高低/整数关口/最近摆动点）"


def _validate_evidence_fields(
    payload: dict[str, object], snapshot: dict[str, object] | None
) -> str | None:
    fields = payload.get("evidence_fields")
    if not isinstance(fields, list) or not fields or len(fields) > MAX_EVIDENCE_FIELDS:
        return "依据字段列表无效：evidence_fields"
    for field in fields:
        if not isinstance(field, str) or not EVIDENCE_FIELD_PATTERN.match(field):
            return "依据字段列表无效：evidence_fields"
    if snapshot is not None:
        for field in fields:
            if not _resolve_evidence_path(snapshot, field):
                return f"依据字段不在已提供事实中：{field}"
    return None


def _validate_trade_geometry(payload: dict[str, object]) -> str | None:
    direction = payload.get("direction")
    if direction == "NEUTRAL":
        return None
    entry = _parse_prices(payload.get("entry_zone"))
    take_profit = _parse_prices(payload.get("take_profit"))
    stop_loss = _parse_prices(payload.get("stop_loss"))
    if not entry:
        return "交易建议字段无法解析为价格：entry_zone"
    if len(take_profit) != 1:
        return "交易建议字段无法解析为价格：take_profit"
    if len(stop_loss) != 1:
        return "交易建议字段无法解析为价格：stop_loss"
    entry_mid = (min(entry) + max(entry)) / 2
    target, floor = take_profit[0], stop_loss[0]
    if direction == "LONG":
        if target <= entry_mid:
            return "多头建议的止盈必须高于入场区间中点"
        if floor >= entry_mid:
            return "多头建议的止损必须低于入场区间中点"
    if direction == "SHORT":
        if target >= entry_mid:
            return "空头建议的止盈必须低于入场区间中点"
        if floor <= entry_mid:
            return "空头建议的止损必须高于入场区间中点"
    return None


def _validate_suggestions(payload: dict[str, object]) -> str | None:
    """可执行建议字段格式：suggestions/scenarios/avoid 必须是非空中文字符串数组。

    suggestions 2-3 条、scenarios 2-3 条、avoid 1-2 条；内容须含中文（防止模型输出英文废话）。
    """
    limits = {"suggestions": (2, 3), "scenarios": (2, 3), "avoid": (1, 2)}
    for key, (lo, hi) in limits.items():
        value = payload.get(key)
        if not isinstance(value, list) or not value:
            return f"建议字段无效：{key}"
        if not (lo <= len(value) <= hi):
            return f"建议字段条数须为 {lo}-{hi} 条：{key}"
        for item in value:
            if not isinstance(item, str) or not item.strip():
                return f"建议字段内容无效：{key}"
            if not re.search(r"[一-鿿]", item):
                return f"建议字段必须使用中文：{key}"
    return None


def _validate_against_snapshot(
    payload: dict[str, object], snapshot: dict[str, object], mode: JobMode = "scalp"
) -> str | None:
    """Magnitude sanity checks of trade levels against the captured bid/ATR."""
    if payload.get("direction") == "NEUTRAL":
        return None
    bid = snapshot.get("bid")
    atr = _reference_atr(snapshot)
    if not isinstance(bid, (int, float)) or atr is None or atr <= 0:
        return None  # 快照缺少基准时跳过幅度校验（几何校验仍已执行）
    entry = _parse_prices(payload.get("entry_zone"))
    take_profit = _parse_prices(payload.get("take_profit"))
    stop_loss = _parse_prices(payload.get("stop_loss"))
    if not entry or len(take_profit) != 1 or len(stop_loss) != 1:
        return None  # 解析失败已由几何校验报告
    entry_mid = (min(entry) + max(entry)) / 2
    _entry_tol, bid_tol, tp_limit, sl_limit = _mode_limits(mode)
    if abs(entry_mid - bid) > bid_tol * atr:
        return "入场区间偏离快照报价超过允许波动"
    if abs(take_profit[0] - entry_mid) > tp_limit * atr:
        return f"止盈距离入场超过 {tp_limit:g} 倍参考 ATR"
    if abs(stop_loss[0] - entry_mid) > sl_limit * atr:
        return f"止损距离入场超过 {sl_limit:g} 倍参考 ATR"
    return None


def validate_report(
    payload: object,
    gate: GateResult,
    snapshot: dict[str, object] | None = None,
    mode: JobMode = "scalp",
) -> tuple[bool, str, dict[str, object] | None]:
    if not isinstance(payload, dict):
        return False, "报告不是 JSON 对象", None
    missing = REQUIRED_KEYS - set(payload)
    if missing:
        return False, f"报告缺少字段：{', '.join(sorted(missing))}", None
    action = payload.get("action")
    if action != "ANALYSE":
        return False, "军师模式报告动作必须是 ANALYSE", None
    # 军师模式：方向/点位纪律（共振相悖、震荡市强方向、RSI 极端追单、
    # 入场不贴关键价位）由硬拒绝改为风险标注，随报告呈现给交易者。
    validation_warnings: list[str] = []
    if gate.directional_plan_allowed:
        trade_missing = TRADE_KEYS - set(payload)
        if trade_missing:
            return False, f"分析模式报告缺少交易建议字段：{', '.join(sorted(trade_missing))}", None
        suggestion_missing = SUGGESTION_KEYS - set(payload)
        if suggestion_missing:
            return False, f"分析模式报告缺少可执行建议字段：{', '.join(sorted(suggestion_missing))}", None
    source_ids = payload.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids or not all(
        isinstance(source, str) for source in source_ids
    ):
        return False, "报告来源标识无效", None
    allowed_sources = allowed_source_ids(snapshot)
    for source in source_ids:
        if source not in allowed_sources:
            return False, f"报告引用了未提供的数据源：{source}", None
    visible_keys = list(VISIBLE_TEXT_KEYS)
    if gate.directional_plan_allowed:
        visible_keys.append("risk_note")
    for key in visible_keys:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            return False, f"报告字段无效：{key}", None
        latin_terms = re.findall(r"\b[A-Za-z][A-Za-z0-9]*\b", payload[key])
        if not re.search(r"[一-鿿]", payload[key]) or any(
            term.upper() not in ALLOWED_LATIN_TERMS for term in latin_terms
        ):
            return False, f"报告正文必须使用中文：{key}", None
    evidence_error = _validate_evidence_fields(payload, snapshot)
    if evidence_error:
        return False, evidence_error, None
    if gate.directional_plan_allowed:
        direction = payload.get("direction")
        if direction not in ALLOWED_DIRECTIONS:
            return False, f"交易方向无效，必须是 {', '.join(sorted(ALLOWED_DIRECTIONS))}", None
        for field in ("entry_zone", "take_profit", "stop_loss"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                return False, f"交易建议字段无效：{field}", None
        geometry_error = _validate_trade_geometry(payload)
        if geometry_error:
            return False, geometry_error, None
        suggestion_error = _validate_suggestions(payload)
        if suggestion_error:
            return False, suggestion_error, None
        if snapshot is not None:
            snapshot_error = _validate_against_snapshot(payload, snapshot, mode)
            if snapshot_error:
                return False, snapshot_error, None
            for warning in (
                _validate_resonance_direction(payload, snapshot),
                _validate_regime_direction(payload, snapshot),
                _validate_entry_at_key_level(payload, snapshot, mode),
            ):
                if warning is not None:
                    validation_warnings.append(warning)
    accepted = dict(payload)
    if gate.warnings:
        accepted["gate_warnings"] = [_clean_warning(w) for w in gate.warnings]
    if validation_warnings:
        accepted["validation_warnings"] = validation_warnings
    return True, "报告已验收", accepted
