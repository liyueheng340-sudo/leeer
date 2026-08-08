"""Deterministic gates for MT5 facts, event context and model eligibility.

系统宪法（docs/xau-system-constitution.md）双层设计：
- 第二条 分析层永不锁死——系统是军师不是保安。闸门只在第一手数据不可用时
  BLOCKED；事件窗口、市场状态、流动性、共振、EA 风控态等一律转为 warnings 标注，
  随报告呈现给交易者，绝不阻断分析，模型照常运行。
- 第九条 入场纪律层（2026-08-03 修正案一）——入场方案基于实证测量证据可被撤销：
  点差高位/scalp 亚洲时段触发 directional_plan_allowed=False（不给方向计划但照常
  出观察，"不给方案"≠"不开口"）。否决权只来自本地实证负期望证据（见各闸门函数
  引用的证据数字），策略意见禁方向仍违宪；拦截原因必须随 gate.warnings 透明呈现。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .regime import compute_market_regime
from .resonance import compute_resonance

GateAction = Literal["ANALYSE", "BLOCKED"]

# 点差峰值达到该值（价格单位）即视为异常扩大，写入标注（剥头皮目标 3-5 个点）。
SPREAD_DOWNGRADE_THRESHOLD = 0.5
# 点差分位硬闸门阈值：当前点差处于近期历史高位（≥80 分位）时禁方向建议。
# 依据（2026-08-03 回测+复盘）：实盘 33 单点差≥80分位胜率仅 12%（-23.3R），
# 点差正常组 46%（+9.9R）；回测证明规则在低点差下才有正期望。
SPREAD_BLOCK_PERCENTILE = 0.8
# 共振明确阈值：|score| ≥ 0.5 视为方向证据明确；否则标注"共振不明确"。
RESONANCE_CLEAR_THRESHOLD = 0.5
# 共振偏空阈值：score ≤ -0.5 且确定性证据下，空头方向历史胜率显著低于多头。
# 依据（2026-08-07 本地复盘 130 单）：SHORT 方向 79 单胜率 30%（-0.18R）vs
# LONG 51 单胜率 55%（+0.30R）；共振偏空 38 单胜率 21%（-0.50R）vs 共振偏多
# 28 单 57%（+0.38R）——共振偏空是最强的单一结构负期望信号之一。
RESONANCE_BEAR_BREAKEVEN_SCORE = -0.5
# 黄金剥头皮活跃时段（校正 UTC）：伦敦 / 伦敦纽约重叠 / 纽约午盘。
ACTIVE_SESSION_LABELS = {"london", "london_ny_overlap", "ny_late"}


@dataclass(frozen=True)
class GateResult:
    action: GateAction
    allow_model: bool
    directional_plan_allowed: bool
    reason: str
    # 风险标注（军师模式）：不阻断分析，随报告呈现给交易者。
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def tick_downgrade_reason(tick_health: dict[str, object] | None) -> str | None:
    """返回 tick 传感器触发的风险标注；传感器不可用时不做判断。"""
    if not isinstance(tick_health, dict) or tick_health.get("available") is not True:
        return None
    if tick_health.get("stalled") is True:
        detail = str(tick_health.get("stall_reason") or "").strip()
        suffix = f"（{detail}）" if detail else ""
        return f"报价流停滞{suffix}"
    # 点差异常已由 spread_block_reason 硬闸门给出"禁方向"标注，
    # 此处不再重复报普通警告（避免同一条点差产生两条标注）。
    if spread_block_reason(tick_health) is not None:
        return None
    return None


def spread_block_reason(tick_health: dict[str, object] | None) -> str | None:
    """点差成本硬闸门：点差处于近期历史高位（≥80 分位）或峰值异常 → 禁方向建议。

    依据（2026-08-03 本地回测 + 实盘复盘）：点差≥80分位的 33 单实盘胜率仅 12%
    （累计 −23.3R），点差正常组 46%（+9.9R）——点差是最大负期望来源。
    只禁方向建议（directional_plan_allowed=False），分析本身不锁死（军师模式）。
    """
    if not isinstance(tick_health, dict) or tick_health.get("available") is not True:
        return None
    percentile = tick_health.get("spread_percentile")
    if isinstance(percentile, (int, float)) and percentile >= SPREAD_BLOCK_PERCENTILE:
        return f"点差处于近期历史高位（{percentile:.0%} 分位），入场成本过高，禁方向建议"
    spread_max = tick_health.get("spread_max")
    if isinstance(spread_max, (int, float)) and spread_max >= SPREAD_DOWNGRADE_THRESHOLD:
        return f"点差异常扩大（峰值 {spread_max:.2f}），入场成本过高，禁方向建议"
    return None


def session_block_reason(snapshot: dict[str, object], mode: str) -> str | None:
    """scalp 模式亚洲时段禁方向：流动性差、点差宽、方向浅且易反转。

    外部共识（pro-scalper/goldscalpers 等多项目）+ 本地复盘（asia 51 单占大头且
    全在亏）一致：剥头皮只在伦敦/重叠/纽约午盘做；亚洲时段只观察。
    swing 模式保留方向（波段可持仓过渡时段）。
    """
    if mode != "scalp":
        return None
    label = snapshot.get("session_label")
    if label == "asia":
        return "亚洲时段流动性不足（剥头皮禁区），禁方向建议，仅观察"
    return None


def resonance_downgrade_reason(resonance: dict[str, object]) -> str | None:
    """共振不明确时标注：只有结构数据存在时才判断，缺失不标注。"""
    score = resonance.get("score")
    if not isinstance(score, (int, float)) or abs(score) >= RESONANCE_CLEAR_THRESHOLD:
        return None
    return f"多周期共振不明确（score {score:+.2f}），方向证据一般"


def bear_bias_downgrade_reason(resonance: dict[str, object]) -> str | None:
    """共振明确偏空时标注：空头方向历史胜率显著低于多头（实证负期望警示）。

    依据（2026-08-07 本地复盘 130 单）：共振偏空 38 单胜率 21%（累计 -0.50R）
    vs 共振偏多 28 单胜率 57%（+0.38R）；SHORT 方向整体 79 单胜率 30%（-0.18R）
    vs LONG 51 单胜率 55%（+0.30R）。当结构明确指向空头（score ≤ -0.5）时，
    除非有极强的宏观/事件证据，空头追入的期望为负——标注供模型与交易者
    作为"空头需更强证据"的纪律提示（军师模式不阻断方向，保留空头自由度）。
    只消费已收盘 K 线的确定性 score，缺失/证据不足不标注。
    """
    score = resonance.get("score")
    if not isinstance(score, (int, float)) or score > RESONANCE_BEAR_BREAKEVEN_SCORE:
        return None
    return (
        f"共振明确偏空（score {score:+.2f}）：本地复盘空头胜率显著低于多头"
        "（SHORT 30% vs LONG 55%），空头建议需更强的宏观/事件证据与更严的风控，"
        "追空负期望风险高"
    )


def short_bias_downgrade_reason(regime: dict[str, object]) -> str | None:
    """强趋势市做空实证警示：本地复盘空头期望为负，且趋势确认多头时追空更危险。

    依据（2026-08-07 本地复盘 130 单）：SHORT 方向 79 单胜率 30%（avg_r -0.18），
    vs LONG 51 单胜率 55%（+0.30R）——空头是本系统当前最深的单一方向负期望。
    军师模式不阻断空头（保留合法做空自由），但给出实证警示让模型在空头时
    更严格地要求证据、更紧地设防。只消费确定性 regime 数据，缺失不标注。
    """
    if regime.get("available") is not True:
        return None
    trend_direction = regime.get("trend_direction")
    if trend_direction == "buy":
        # 强趋势多头市做空 = 逆势 + 空头历史弱，双重负期望
        return (
            "强趋势多头市（本地复盘空头胜率 30% vs 多头 55%）：逆势做空"
            "叠加空头历史弱期望，除非有极强宏观/事件证据，追空风险极高"
        )
    if trend_direction == "sell":
        # 强趋势空头市做空 = 顺势但空头历史弱，仍需谨慎
        return (
            "强趋势空头市做空：虽顺势，但本地复盘空头胜率 30% 显著低于多头 55%，"
            "空头建议仍需更强的证据与更严的风控"
        )
    return None


def regime_downgrade_reason(regime: dict[str, object]) -> str | None:
    """震荡市标注：双周期（M15+H1）ADX 均 < 20 时方向证据不足。

    EA 精华：趋势跟踪只在 ADX ≥ 25 的强趋势市开单；震荡市追单/开仓胜率
    天然偏低。标注供交易者参考，不阻断方向建议。
    只消费已收盘 K 线的确定性指标，指标缺失时不标注。
    """
    if regime.get("available") is not True or regime.get("regime") != "ranging":
        return None
    return "市场处于震荡市（双周期 ADX < 20），趋势方向证据不足"


# ATR 防追价阈值（king-v2 PriceNotExtended 精华）：价格离 EMA20 超过该倍数
# ATR 即视为"价格延伸过度"，追价负期望（EA 精华：只吃回调不追价）。
EMA_EXTENSION_WARN_ATR = 2.5


def ema_extension_reason(regime: dict[str, object]) -> str | None:
    """价格延伸过度标注（king-v2 PriceNotExtended 精华）：禁追价警示。

    king-v2 用 PriceNotExtended 拒绝离均线过远的入场；这里用 EMA20 距离归一化
    为 ATR 倍数，超过阈值即标注"价格延伸过度，追价风险高"。
    只消费已收盘 K 线的确定性指标，指标缺失时不标注。
    """
    if regime.get("available") is not True:
        return None
    extension = regime.get("ema_extension")
    if not isinstance(extension, dict):
        return None
    distance = extension.get("atr_distance")
    if not isinstance(distance, (int, float)) or distance < EMA_EXTENSION_WARN_ATR:
        return None
    side = extension.get("side")
    side_text = "价格高于 EMA20" if side == "above" else "价格低于 EMA20"
    return f"价格延伸过度（{side_text} {distance:g} 倍 ATR），追价风险高，宜等回调"


def cci_extreme_reason(regime: dict[str, object]) -> str | None:
    """CCI 轨外标注（恒鑫 EA 精华：±100 轨外不开首单）。

    恒鑫 EA 用 CCI ±100 作为首单过滤：轨外（≥100 或 ≤-100）视为价格延伸过度、
    回归概率上升，禁开新单。这里转为风险标注（军师模式不阻断分析）。
    """
    if regime.get("available") is not True:
        return None
    extreme = regime.get("cci_extreme")
    if not isinstance(extreme, dict):
        return None
    side = extreme.get("side")
    value = extreme.get("value")
    if side == "overbought" and isinstance(value, (int, float)):
        return f"CCI {value:.0f} 突破上轨（≥100，恒鑫过滤），价格延伸过度，追多风险高"
    if side == "oversold" and isinstance(value, (int, float)):
        return f"CCI {value:.0f} 跌破下轨（≤-100，恒鑫过滤），价格延伸过度，追空风险高"
    return None


def session_downgrade_reason(snapshot: dict[str, object]) -> str | None:
    """非活跃时段流动性不足标注；缺失 session_label 不标注。"""
    label = snapshot.get("session_label")
    if not isinstance(label, str) or not label:
        return None
    if label not in ACTIVE_SESSION_LABELS:
        return f"当前时段 {label} 流动性不足，注意点差与滑点"
    return None


def ea_downgrade_reason(ea_status: dict[str, object] | None) -> str | None:
    """返回 Cerberus EA 风控态触发的风险标注；无需标注时返回 None。

    只消费风险机制字段（status / regime_blocked / hour_blocked），绝不读取
    持仓与盈亏——后者是事后测量，不构成预测证据（HY3 纪律）。所有风险机制
    均转为标注（军师模式不阻断）：PAUSED_NEWS / PAUSED_VOLATILITY /
    regime_blocked / hour_blocked 随报告呈现。PAUSED_MANUAL / PAUSED_SCHEDULE
    是操作选择而非市场证据，不标注。
    """
    if not isinstance(ea_status, dict) or ea_status.get("available") is not True:
        return None
    status = ea_status.get("status")
    if status == "PAUSED_NEWS":
        return "EA 风控处于新闻事件窗口（Cerberus），波动可能剧烈"
    reasons: list[str] = []
    if status == "PAUSED_VOLATILITY":
        reasons.append("EA 风控触发波动率熔断")
    if ea_status.get("regime_blocked") is True:
        reasons.append("EA 报告 H1 强趋势机制（趋势否决生效）")
    if ea_status.get("hour_blocked") is True:
        reasons.append("EA 报告当前时段为高危波动窗口")
    if reasons:
        return "；".join(reasons)
    return None


def evaluate_gate(
    snapshot: dict[str, object],
    event_context: dict[str, object],
    now: datetime,
    tick_health: dict[str, object] | None = None,
    ea_status: dict[str, object] | None = None,
    resonance: dict[str, object] | None = None,
    regime: dict[str, object] | None = None,
    mode: str = "scalp",
) -> GateResult:
    if snapshot.get("identity_match") is not True or snapshot.get("symbol") != "XAUUSD":
        return GateResult("BLOCKED", False, False, "MT5 经纪商或品种身份不匹配")
    if not valid_quote(snapshot):
        return GateResult("BLOCKED", False, False, "MT5 报价不可用")
    if snapshot_age_seconds(snapshot, now) > 60:
        return GateResult("BLOCKED", False, False, "MT5 快照已超过 60 秒")
    # 军师模式：事件窗口/未核验只标注，绝不锁死（宪法第 1、2 条）。
    warnings: list[str] = []
    if event_context.get("status") == "wait":
        reason = str(event_context.get("reason") or "已核验的高影响事件窗口")
        # reason 已含"高影响事件窗口"前缀（calendar 构造），此处不再重复包裹。
        warnings.append(reason)
    elif event_context.get("status") != "verified_clear":
        unverified_reason = str(event_context.get("reason") or "").strip()
        suffix = f"（{unverified_reason}）" if unverified_reason else ""
        warnings.append(f"事件上下文未核验，事件驱动信息仅供参考{suffix}")
    # 共振/市场状态只算一次：调用方（service）可预计算传入，此处仅兜底。
    if resonance is None:
        resonance = compute_resonance(snapshot)
    if regime is None:
        regime = compute_market_regime(snapshot)
    # 入场纪律硬闸门（2026-08-03 升级）：点差高位 / scalp 亚洲时段 → 禁方向。
    # 分析保留（allow_model=True），只禁方向建议——军师模式不锁死分析，但入场
    # 成本与时段是实盘负期望的根源（复盘 -23.3R vs +9.9R；回测 71% vs 52%）。
    directional_allowed = True
    for block_reason in (
        spread_block_reason(tick_health),
        session_block_reason(snapshot, mode),
    ):
        if block_reason is not None:
            directional_allowed = False
            warnings.append(block_reason)
    # 收集全部风险标注：EA 风控、tick 健康、共振、市场状态、时段流动性。
    for warn in (
        ea_downgrade_reason(ea_status),
        tick_downgrade_reason(tick_health),
        resonance_downgrade_reason(resonance),
        bear_bias_downgrade_reason(resonance),
        regime_downgrade_reason(regime),
        short_bias_downgrade_reason(regime),
        ema_extension_reason(regime),
        cci_extreme_reason(regime),
        session_downgrade_reason(snapshot),
    ):
        if warn is not None:
            warnings.append(warn)
    if not directional_allowed:
        reason = f"MT5 快照新鲜；分析可用，入场纪律拦截（{len(warnings)} 条标注）"
    elif warnings:
        reason = f"MT5 快照新鲜；分析可用，附带 {len(warnings)} 条风险标注"
    else:
        reason = "MT5 快照新鲜且事件状态已核验，无风险标注"
    return GateResult("ANALYSE", True, directional_allowed, reason, warnings=tuple(warnings))


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
