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
)
from .snapshot_facts import _key_levels, _parse_prices, _reference_atr

# 证据路径格式：字母数字下划线点，加上列表索引 'items[0]' / 'items[]'。
EVIDENCE_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9_.\[\]]+$")
# 证据字段上限：模型按 facts_paths 清单引用真实字段，实测常达 15 个；
# 12 过严会拒绝全合法的报告（2026-08-03 高频 REJECTED 根因），放宽到 20 仍防滥用。
MAX_EVIDENCE_FIELDS = 20
# 证据路径列表索引：'items[0]'（数字）或 'items[]'（通配）。EVIDENCE_FIELD_PATTERN
# 允许方括号字符，故此处单独解析索引段。
_INDEX_PATTERN = re.compile(r"^(?P<key>[A-Za-z0-9_]+)\[(?P<index>\d*)\]$")

# 叙述性默认文案（2026-08-02 放宽）：invalidation / next_observation 缺失或空值时
# 自动补默认值通过验收——模型漏写描述性内容不影响真实性，不必整条重试拖慢速度。
NARRATIVE_DEFAULTS: dict[str, str] = {
    "invalidation": "数据过期、身份不匹配或事件状态变化时失效。",
    "next_observation": "等待价格触及关键区间或结构变化后再评估。",
}


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
    """True when a dotted evidence path ('timeframe_structure.h1.atr_14') exists in facts.

    支持列表索引两种写法：'news_context.items[0].title'（数字索引）与
    'news_context.items[].title'（通配索引，提示词示例中的写法）。索引越界返回 False。
    """
    from typing import Any

    current: Any = snapshot
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        # 列表索引：items[0] / items[]
        match = _INDEX_PATTERN.fullmatch(part)
        if match is None or not isinstance(current, dict):
            return False
        key = match.group("key")
        if key not in current or not isinstance(current[key], list):
            return False
        items = current[key]
        index = match.group("index")
        if index == "":
            return bool(items)  # 通配 []：列表非空即有效
        position = int(index)
        if position < 0 or position >= len(items):
            return False
        current = items[position]
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


def _validate_pullback_form(
    payload: dict[str, object], snapshot: dict[str, object], mode: JobMode = "scalp"
) -> str | None:
    """scalp 顺势回调形态校验：入场在区间回调位为佳，追价降级为风险标注。

    依据（2026-08-03 本地回测）：回调入场 52.6% 胜率 / +0.21R vs 追价 23.1% /
    −0.43R。但 2026-08-03 用户反馈"简报经常要点几次才出来"——追价硬拒把
    "低质量建议"变成"无建议"，交易者连简报都拿不到。修正为军师模式：
    **追价降级为 validation_warnings 随报告呈现**（低质量如实标注，不阻断简报），
    与共振相悖/震荡市强方向等纪律标注同层。回测信号仍通过 prompt 纪律传达。
    """
    if mode != "scalp" or payload.get("direction") == "NEUTRAL":
        return None
    direction = payload.get("direction")
    entry = _parse_prices(payload.get("entry_zone"))
    if not entry:
        return None
    structure = snapshot.get("timeframe_structure")
    loc = None
    if isinstance(structure, dict):
        frame = structure.get("m5") or structure.get("m15")
        if isinstance(frame, dict) and isinstance(frame.get("range_location_8"), (int, float)):
            loc = float(frame["range_location_8"])
    if loc is None:
        return None  # 缺 range_location 数据时跳过（几何/关键价位校验仍生效）
    # 追价判定：LONG 在区间高位（loc≥0.65）/ SHORT 在区间低位（loc≤0.35）即追价。
    # 阈值取 0.65/0.35（回测中 0.4/0.6 为回调过滤边界，验收留一点缓冲避免误杀）。
    if direction == "LONG" and loc >= 0.65:
        return f"做多入场在区间高位（range_location_8={loc:.2f}），追高胜率偏低（回调纪律）"
    if direction == "SHORT" and loc <= 0.35:
        return f"做空入场在区间低位（range_location_8={loc:.2f}），追低胜率偏低（回调纪律）"
    return None


def _validate_evidence_fields(
    payload: dict[str, object], snapshot: dict[str, object] | None
) -> tuple[str | None, list[str]]:
    """Validate evidence_fields; returns (hard_error, warnings).

    军师模式分级（2026-08-03 修复，实测 brief 高频 REJECTED 根因）：
    - 硬拒（结构/格式错误）：不是列表、空、超限、字段非字符串、含非法字符——
      这类是"模型没按契约输出"，重试比降级更有价值；
    - 警告（引用瑕疵）：路径格式合法但未解析到已提供事实（含列表索引越界）——
      模型引用真实事实时猜错路径/索引是常见小错，降级为风险标注随报告呈现，
      不整单拒绝（否则 DeepSeek/GLM 引用 items[1] 越界或短名路径就必被拒）。
    """
    fields = payload.get("evidence_fields")
    if not isinstance(fields, list) or not fields:
        return "依据字段列表无效：evidence_fields", []
    # 超限容错（2026-08-04 复审实测）：模型合法引用 21 个字段被硬拒——今日
    # 两单自动调度简报均命中（观察报告事实字段多，合法引用就易超 20 个）。
    # 截前 N 个并标注，不整单拒绝：引用完整性是展示问题，不是真实性问题。
    warnings: list[str] = []
    if len(fields) > MAX_EVIDENCE_FIELDS:
        warnings.append(
            f"evidence_fields 超过 {MAX_EVIDENCE_FIELDS} 个（{len(fields)} 个），已截取前 {MAX_EVIDENCE_FIELDS} 个"
        )
        fields = fields[:MAX_EVIDENCE_FIELDS]
        payload["evidence_fields"] = fields
    for field in fields:
        if not isinstance(field, str) or not EVIDENCE_FIELD_PATTERN.match(field):
            return "依据字段列表无效：evidence_fields", []
        if snapshot is None:
            continue
        if not _resolve_evidence_path(snapshot, field):
            warnings.append(f"依据字段未解析到已提供事实：{field}")
    return None, warnings


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
    loose_evidence: bool = False,
) -> tuple[bool, str, dict[str, object] | None]:
    if not isinstance(payload, dict):
        return False, "报告不是 JSON 对象", None
    # 2026-08-02 放宽：叙述性字段缺失/空值时自动补默认文案，不拒绝、不重试。
    for _key, _default in NARRATIVE_DEFAULTS.items():
        if not isinstance(payload.get(_key), str) or not payload[_key].strip():
            payload[_key] = _default
    # summary 兜底（2026-08-03 余量）：模型偶发漏 summary（实测 3 次 REJECTED 根因），
    # 但 direction/invalidation/next_observation 常已给出。用这些字段拼一个简短
    # 摘要，保证简报有内容可展示——"宁给低置信度建议，不可给没有建议"（宪法第二条）。
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        parts = []
        direction = payload.get("direction")
        if direction == "LONG":
            parts.append("当前倾向偏多")
        elif direction == "SHORT":
            parts.append("当前倾向偏空")
        elif direction == "NEUTRAL":
            parts.append("当前倾向观望")
        for key in ("invalidation", "next_observation"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(str(val).strip())
        payload["summary"] = "；".join(parts) if parts else "当前无明确方向，建议等待结构确认。"
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
    # 真实性底线（2026-08-02 放宽）：只要求报告锚定真实数据源 mt5_snapshot。
    # 其余来源名不再逐一拒绝——模型用短名（tick_health/session_context 等）
    # 引用快照内嵌事实是合理写法；内容真实性由 evidence_fields 路径校验承担。
    if "mt5_snapshot" not in source_ids:
        return False, "报告必须引用 mt5_snapshot 作为数据锚点", None
    visible_keys = list(VISIBLE_TEXT_KEYS)
    if gate.directional_plan_allowed:
        visible_keys.append("risk_note")
    for key in visible_keys:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            return False, f"报告字段无效：{key}", None
        latin_terms = re.findall(r"\b[A-Za-z][A-Za-z0-9]*\b", payload[key])
        # 中文正文是真实性底线，保留硬约束（防模型输出全英文）。
        if not re.search(r"[一-鿿]", payload[key]):
            return False, f"报告正文必须使用中文：{key}", None
        # 白名单外英文词（2026-08-03 余量）：从硬拒改为风险标注——模型偶发引入
        # 合法宏观缩写（ISM/PMI/UTC 等）或个别普通词，不应整单拒绝（实测 3 次
        # REJECTED 根因）。如实标注，让交易者拿到简报自己判断。
        unknown = [term for term in latin_terms if term.upper() not in ALLOWED_LATIN_TERMS]
        if unknown:
            validation_warnings.append(f"{key} 含白名单外英文词：{', '.join(sorted(set(unknown)))}，建议用中文表述")
    # 证据路径校验分级：结构/格式错误硬拒；引用瑕疵（路径未解析）转风险标注。
    evidence_error, evidence_warnings = _validate_evidence_fields(payload, snapshot)
    if evidence_error and not loose_evidence:
        return False, evidence_error, None
    validation_warnings.extend(evidence_warnings)
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
            # 追价形态纪律（1.8.0）：scalp 追高/追低降级为风险标注，不拒绝报告
            # （2026-08-03 用户反馈"简报经常要点几次才出来"——硬拒把低质量建议
            # 变成无建议；军师模式如实标注，让交易者拿到简报自己判断）。
            pullback_warning = _validate_pullback_form(payload, snapshot, mode)
            if pullback_warning:
                validation_warnings.append(pullback_warning)
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
