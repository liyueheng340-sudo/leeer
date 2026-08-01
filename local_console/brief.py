"""Constrained Qwen research with a deterministic report acceptance gate."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from tradingagents.llm_clients.factory import create_llm_client

from .config import ConsoleConfig
from .guard import GateResult
from .jobs import JobKind

REQUIRED_KEYS = {
    "action",
    "source_ids",
    "summary",
    "invalidation",
    "next_observation",
    "evidence_fields",
}
TRADE_KEYS = {"direction", "entry_zone", "take_profit", "stop_loss", "risk_note"}
VISIBLE_TEXT_KEYS = ("summary", "invalidation", "next_observation")
MODEL_TIMEOUT_SECONDS = 90
# 单次瞬时失败（网络抖动 / 偶发坏 JSON）不应让整个任务失败：
# 在应用层做有限重试（客户端层 max_retries 保持 0，避免双层重试叠加）。
MODEL_MAX_RETRIES = 1
MODEL_RETRY_BACKOFF_SECONDS = 5
# 深度复盘用的是推理模型，显著比快评的轻量模型慢（实测常态 55-90 秒、偶尔更长）：
# 若沿用快评的 90 秒上限，会在临界点频繁超时失败，故单独放宽到 180 秒。
DEEP_MODEL_TIMEOUT_SECONDS = 180
# 深度复盘超时几乎总是“模型持续偏慢”而非瞬时抖动，重试只会让唯一 worker 再被独占
# 三分钟、阻塞期间所有快评与自主调度，故深度复盘不做应用层重试（快评保留重试）。
DEEP_MODEL_MAX_RETRIES = 0
# 提示词版本：每次修改 build_prompt 的约束/规则后手动递增，
# 随任务落盘（gate_payload.prompt_version），供复盘关联“改提示词前后”的效果差异。
# 1.1.1：针对实测 REJECTED 主因（risk_note/next_observation 混入英文单词）补充白名单缩写规则。
# 1.3.0：按实测复盘（ANALYSE 组 avg R -0.23 vs WATCH 组 +0.54）收紧强方向输出——
#       共振不明确/非活跃时段/点差超标时 guard 降级；报告方向与共振相悖直接 REJECT；
#       关键价位层（前日高低/当日高低/整数关口/最近摆动点）喂入 prompt 并要求入场贴近。
PROMPT_VERSION = "1.3.0"
ALLOWED_ACTIONS = {"ANALYSE", "WATCH", "WAIT"}
ALLOWED_DIRECTIONS = {"LONG", "SHORT", "NEUTRAL"}
ALLOWED_LATIN_TERMS = {
    "ASK",
    "ATR",
    "BEA",
    "BID",
    "BITGET",
    "COT",
    "CPI",
    "DFII10",
    "DGS10",
    "DTWEXBGS",
    "DXY",
    "ETF",
    "FOMC",
    "FRED",
    "GDP",
    "GLD",
    "H1",
    "H4",
    "LONG",
    "M1",
    "M5",
    "M15",
    "MT5",
    "NEUTRAL",
    "NFP",
    "PCE",
    "QWEN",
    "SHORT",
    "SL",
    "T10YIE",
    "TIPS",
    "TP",
    "WATCH",
    "XAUUSD",
}
DIRECT_ENTRY_PATTERN = re.compile(
    r"\b(buy|sell|long|short)\s+(now|immediately)\b|立即买入|立即卖出|立即开多|立即开空|马上买入|马上卖出",
    re.IGNORECASE,
)
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
EVIDENCE_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9_.]+$")
MAX_EVIDENCE_FIELDS = 12
# 数值一致性阈值（以参考 ATR 为单位）
ENTRY_BID_ATR_TOLERANCE = 3.0
TAKE_PROFIT_ATR_LIMIT = 5.0
STOP_LOSS_ATR_LIMIT = 3.0


def request_brief(
    config: ConsoleConfig,
    kind: JobKind,
    snapshot: dict[str, object],
    gate: GateResult,
) -> object:
    is_deep = kind == "deep_review"
    model = config.deep_model if is_deep else config.quick_model
    timeout = DEEP_MODEL_TIMEOUT_SECONDS if is_deep else MODEL_TIMEOUT_SECONDS
    max_retries = DEEP_MODEL_MAX_RETRIES if is_deep else MODEL_MAX_RETRIES
    llm = create_llm_client(
        "qwen", model, config.backend_url, timeout=timeout, max_retries=0
    ).get_llm()
    return _invoke_with_retry(llm.invoke, build_prompt(snapshot, gate, kind), max_retries)


def _parse_model_json(content: str) -> object:
    """解析模型输出的 JSON，容忍 markdown 围栏与前后夹带文字。

    提示词要求裸 JSON，但模型偶发用 ```json 围栏或夹带短句；宽松解析把这类
    "形式瑕疵"从任务失败转为可用，内容契约仍由 validate_report 严格把关。
    解析候选依次：原文 → 围栏内文本 → 首个 { 至末个 } 的子串。
    """
    text = content.strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as error:
            last_error = error
    raise RuntimeError("Qwen response is not JSON") from last_error


def _invoke_with_retry(
    invoke: Any, prompt: str, max_retries: int = MODEL_MAX_RETRIES
) -> object:
    """调用模型并解析 JSON，瞬时失败时退避重试有限次。

    抽出为纯函数（接受任意 invoke 可调用对象）以便无需真实 LLM 即可测试重试逻辑。
    max_retries 按任务类型传入：快评容忍瞬时抖动可重试，深度复盘超时多为持续偏慢不重试。
    """
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        if attempt:
            time.sleep(MODEL_RETRY_BACKOFF_SECONDS)
        try:
            response = invoke(prompt)
            content = getattr(response, "content", response)
            if not isinstance(content, str):
                raise RuntimeError("Qwen response is not text")
            return _parse_model_json(content)
        except Exception as error:  # 网络/超时/偶发坏 JSON 等瞬时故障
            last_error = error
    raise RuntimeError("Qwen 分析多次重试仍失败") from last_error


def worst_case_seconds(kind: JobKind) -> float:
    """模型阶段最坏耗时（每次超时 × 尝试次数 + 重试退避）。

    供陈旧任务阈值取两种任务的最大值，确保深度复盘的长推理不会被误判为超时。
    """
    if kind == "deep_review":
        timeout, retries = DEEP_MODEL_TIMEOUT_SECONDS, DEEP_MODEL_MAX_RETRIES
    else:
        timeout, retries = MODEL_TIMEOUT_SECONDS, MODEL_MAX_RETRIES
    return timeout * (retries + 1) + MODEL_RETRY_BACKOFF_SECONDS * retries


def allowed_source_ids(gate: GateResult, snapshot: dict[str, object] | None = None) -> set[str]:
    """Source ids a report may cite, derived from the gate and available facts."""
    allowed = {"mt5_snapshot"}
    if gate.action == "ANALYSE":
        allowed.add("verified_event_context")
    macro = (snapshot or {}).get("background_macro")
    if isinstance(macro, dict) and macro.get("status") == "ok":
        allowed.add("fred_macro_background")
    tick = (snapshot or {}).get("tick_health")
    if isinstance(tick, dict) and tick.get("available") is True:
        allowed.add("mt5_tick_health")
    news = (snapshot or {}).get("news_context")
    if isinstance(news, dict) and news.get("status") == "ok" and news.get("items"):
        allowed.add("news_context")
    return allowed


def build_prompt(snapshot: dict[str, object], gate: GateResult, kind: JobKind) -> str:
    allowed_sources = sorted(allowed_source_ids(gate, snapshot))
    output_rules = [
        "Return one JSON object and no markdown.",
        "所有 summary、invalidation、next_observation、risk_note 字段必须使用简体中文；不得用英文输出。",
        "中文字段里只允许出现白名单英文缩写（如 TP、SL、ATR、XAUUSD、DXY、FRED、FOMC、CPI、NFP、PCE、M5、M15、H1、H4）；"
        "其它任何英文单词（包括 risk、note、support、resistance 等普通词）都会导致报告被拒绝。",
        "Use only allowed_sources.",
        "Do not claim an unprovided price, indicator, news item, or event.",
        "Do not promise returns or describe automated execution.",
        "When directional_plan_allowed is false, provide observation and wait conditions only.",
        f"The 'action' field in your output MUST be \"{gate.action}\" (matching gate_action).",
        "evidence_fields 必须列出结论所依据的 facts 字段路径（例如 'bid'、'timeframe_structure.h1.atr_14'），"
        "不得虚构不存在的字段。",
    ]
    macro = snapshot.get("background_macro")
    if isinstance(macro, dict) and macro.get("status") == "ok":
        output_rules.append(
            "background_macro 为日频/周频宏观背景，仅用于中期背景判断，"
            "不得用于描述盘中价位或分钟级结构。"
        )
    event_context = snapshot.get("event_context")
    if isinstance(event_context, dict):
        if event_context.get("status") == "verified_clear":
            output_rules.append(
                "event_context 为已核验的事件日历，包含 current_utc（当前时间锚点）、"
                "next_event（未来高影响事件）和 past_events（最近 48 小时内已公布的高影响事件）。"
                "规则："
                "① 若 next_event 存在，在 risk_note 中说明下一事件时间与潜在冲击；"
                "② 若 past_events 中存在某事件，该事件已公布，不得再说'即将公布'，"
                "应视为已落地并评估其市场影响；"
                "③ 日历日期可能与实际公布时间有偏差，若 news_context 中已有某事件的报道"
                "（如数据已发布/已公布），以新闻为准，不得再说'即将公布'；"
                "④ 止盈/止损的设置应避免让建议持仓跨越 next_event 窗口。"
            )
        else:
            output_rules.append(
                "event_context 状态为未核验或等待，无法确认事件环境，"
                "不得在报告中声称任何具体事件或其影响。"
            )
    resonance = snapshot.get("timeframe_resonance")
    if isinstance(resonance, dict) and resonance.get("available") is True:
        output_rules.append(
            "timeframe_resonance 是由已收盘 K 线计算的确定性方向共振事实："
            "score∈[-1,1]（正为多、负为空），label 为共振偏多/共振偏空/方向冲突/方向不明。"
            "direction 必须与 score 符号一致：score>0 只允许 LONG 或 NEUTRAL，score<0 只允许 SHORT 或 NEUTRAL；"
            "相悖方向（空头结构做多或多头结构做空）将被直接拒绝。"
            "label 为方向冲突或方向不明（|score|<0.5）时，不得给出强方向建议，宜倾向 NEUTRAL。"
        )
    news = snapshot.get("news_context")
    if isinstance(news, dict) and news.get("status") == "ok" and news.get("items"):
        output_rules.append(
            "news_context 是近期新闻背景，仅作'预期差/是否已定价'的参考："
            "须结合快照的最新结构判断各头条是否已反映在价位中，"
            "不得对新闻做反应式追单，不得声称未经核验的事件冲击，"
            "不得用新闻描述分钟级盘中价位；若 direction 与新闻基调背离，必须在 risk_note 中说明。"
        )
    required_keys = sorted(REQUIRED_KEYS)
    trade_plan_schema = None
    if gate.directional_plan_allowed:
        required_keys = sorted(REQUIRED_KEYS | TRADE_KEYS)
        trade_plan_schema = {
            "direction": "LONG 或 SHORT 或 NEUTRAL（字符串）",
            "entry_zone": "建议入场价格区间，如 '4070-4078'（字符串）",
            "take_profit": "建议止盈价位，如 '4098'（字符串）",
            "stop_loss": "建议止损价位，如 '4055'（字符串）",
            "risk_note": "风险提示，简体中文，一句话",
        }
        key_levels = _key_levels(snapshot)
        if key_levels:
            output_rules.append(
                "关键价位层（由已收盘 K 线与整数关口确定性计算）："
                + "、".join(str(level) for level in key_levels)
                + "。入场区间中点必须贴近其中至少一个关键价位（1 倍参考 ATR 内），否则报告将被拒绝；"
                "支撑位做多、阻力位做空，止损放在关键价位外侧。"
            )
        trade_rule = (
            "You MUST provide direction/entry_zone/take_profit/stop_loss/risk_note. "
            "Base them strictly on the provided snapshot facts (ATR, structure, support/resistance from closed bars). "
            "TP/SL must be concrete price levels derivable from the data. "
            "Use ATR multiples or key support/resistance levels from the closed bars to set TP and SL."
        )
        if gate.action == "WATCH":
            trade_rule += (
                " NOTE: event context is unverified, so add a caution in risk_note that this is a "
                "technical-only suggestion without event confirmation."
            )
        output_rules.append(trade_rule)
    contract = {
        "role": "XAU manual analysis assistant",
        "output_language": "Simplified Chinese",
        "task_kind": kind,
        "gate_action": gate.action,
        "directional_plan_allowed": gate.directional_plan_allowed,
        "allowed_sources": allowed_sources,
        "facts": snapshot,
        "required_json_keys": required_keys,
        "evidence_fields_schema": "结论依据的 facts 字段路径列表，1-12 个字符串，如 ['bid', 'timeframe_structure.h1.atr_14']",
        "output_rules": output_rules,
    }
    if trade_plan_schema:
        contract["trade_plan_schema"] = trade_plan_schema
    return json.dumps(contract, ensure_ascii=False)


def _parse_prices(value: object) -> list[float]:
    """Extract numeric price levels from a free-form string ('4070-4078' -> [4070.0, 4078.0])."""
    if not isinstance(value, str):
        return []
    return [float(token) for token in NUMBER_PATTERN.findall(value)]


def _reference_atr(snapshot: dict[str, object]) -> float | None:
    structure = snapshot.get("timeframe_structure")
    if isinstance(structure, dict):
        for timeframe in ("h1", "m15", "h4"):
            frame = structure.get(timeframe)
            if isinstance(frame, dict) and isinstance(frame.get("atr_14"), (int, float)):
                return float(frame["atr_14"])
    fallback = snapshot.get("atr_m15")
    if isinstance(fallback, (int, float)):
        return float(fallback)
    return None


# 关键价位判定：入场区间中点需落在某关键价位 ± 1.0 ATR 内（剥头皮 M1，取 H1 ATR 作尺度）。
KEY_LEVEL_ATR_TOLERANCE = 1.0


def _key_levels(snapshot: dict[str, object]) -> list[float]:
    """从快照确定性提取关键价位：前日高/低、当日高/低、最近整数关口、最近摆动点。

    全部来自已提供事实（latest_closed_bars / timeframe_structure 的最近已收盘 K 线），
    无模型推断；任一数据缺失即跳过该项。返回去重升序价位列表。
    """
    levels: list[float] = []
    bars = snapshot.get("latest_closed_bars")
    if isinstance(bars, dict):
        for tf in ("h4", "h1", "m15", "m5"):
            bar = bars.get(tf)
            if isinstance(bar, dict):
                high, low = bar.get("high"), bar.get("low")
                if isinstance(high, (int, float)):
                    levels.append(float(high))
                if isinstance(low, (int, float)):
                    levels.append(float(low))
    structure = snapshot.get("timeframe_structure")
    if isinstance(structure, dict):
        for tf in ("h1", "m15", "m5"):
            frame = structure.get(tf)
            if isinstance(frame, dict):
                high, low = frame.get("high"), frame.get("low")
                if isinstance(high, (int, float)):
                    levels.append(float(high))
                if isinstance(low, (int, float)):
                    levels.append(float(low))
    bid = snapshot.get("bid")
    if isinstance(bid, (int, float)) and bid > 0:
        for increment in (50, 100):
            base = round(float(bid) / increment) * increment
            levels.extend([base - increment, base, base + increment])
    unique = sorted(set(round(level, 2) for level in levels if level > 0))
    return unique[:16]  # 防 prompt 膨胀，最多 16 个


def _validate_resonance_direction(
    payload: dict[str, object], snapshot: dict[str, object]
) -> str | None:
    """强方向建议必须与多周期共振同向；相悖直接拒绝（顺势硬约束）。

    实测复盘：SHORT（顺势）30 样本 avg R +0.40，LONG（逆势）7 样本 -0.28。
    共振由已收盘 K 线确定性计算，非模型推断，可作为验收硬规则。
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
        return "方向与多周期共振相悖：共振偏空时禁止做多"
    if direction == "SHORT" and score > 0:
        return "方向与多周期共振相悖：共振偏多时禁止做空"
    return None


def _validate_entry_at_key_level(
    payload: dict[str, object], snapshot: dict[str, object]
) -> str | None:
    """强方向建议的入场必须贴近关键价位（支撑买/阻力卖是剥头皮点位纪律）。

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
    tolerance = KEY_LEVEL_ATR_TOLERANCE * atr if isinstance(atr, (int, float)) and atr > 0 else 2.0
    entry_mid = (min(entry) + max(entry)) / 2
    if any(abs(entry_mid - level) <= tolerance for level in levels):
        return None
    return "入场区间未贴近任何关键价位（前日高低/当日高低/整数关口/最近摆动点）"


def _resolve_evidence_path(snapshot: dict[str, object], path: str) -> bool:
    """True when a dotted evidence path ('timeframe_structure.h1.atr_14') exists in facts."""
    current: Any = snapshot
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return current is not None


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


def _validate_against_snapshot(
    payload: dict[str, object], snapshot: dict[str, object]
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
    if abs(entry_mid - bid) > ENTRY_BID_ATR_TOLERANCE * atr:
        return "入场区间偏离快照报价超过允许波动"
    if abs(take_profit[0] - entry_mid) > TAKE_PROFIT_ATR_LIMIT * atr:
        return "止盈距离入场超过 5 倍参考 ATR"
    if abs(stop_loss[0] - entry_mid) > STOP_LOSS_ATR_LIMIT * atr:
        return "止损距离入场超过 3 倍参考 ATR"
    return None


def validate_report(
    payload: object,
    gate: GateResult,
    snapshot: dict[str, object] | None = None,
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
    if gate.directional_plan_allowed:
        trade_missing = TRADE_KEYS - set(payload)
        if trade_missing:
            return False, f"分析模式报告缺少交易建议字段：{', '.join(sorted(trade_missing))}", None
    source_ids = payload.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids or not all(
        isinstance(source, str) for source in source_ids
    ):
        return False, "报告来源标识无效", None
    allowed_sources = allowed_source_ids(gate, snapshot)
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
    visible_text = " ".join(
        str(payload[key]) for key in ("summary", "invalidation", "next_observation")
    )
    if not gate.directional_plan_allowed and DIRECT_ENTRY_PATTERN.search(visible_text):
        return False, "观察模式报告包含直接入场指令", None
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
        if snapshot is not None:
            snapshot_error = _validate_against_snapshot(payload, snapshot)
            if snapshot_error:
                return False, snapshot_error, None
            resonance_error = _validate_resonance_direction(payload, snapshot)
            if resonance_error:
                return False, resonance_error, None
            level_error = _validate_entry_at_key_level(payload, snapshot)
            if level_error:
                return False, level_error, None
    return True, "报告已验收", dict(payload)
