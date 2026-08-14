"""Deterministic gates for MT5 facts, event context and model eligibility.

系统宪法（docs/xau-system-constitution.md）双层设计：
- 第二条 分析层永不锁死——系统是军师不是保安。闸门只在第一手数据不可用时
  BLOCKED；事件窗口、市场状态、流动性、共振、EA 风控态等一律转为 warnings 标注，
  随报告呈现给交易者，绝不阻断分析，模型照常运行。
- 第九条 入场纪律层（2026-08-03 修正案一）——入场方案基于实证测量证据可被撤销：
  scalp 亚洲时段触发 directional_plan_allowed=False（不给方向计划但照常出观察，
  "不给方案"≠"不开口"）。否决权只来自本地实证负期望证据（见各闸门函数引用的
  证据数字），策略意见禁方向仍违宪；拦截原因必须随 gate.warnings 透明呈现。
  （2026-08-07 修正案二：点差硬闸门经剥离 8/3 伪影复算后证据不足，降级为软标注；
  "空头整体弱"警示同样被证伪撤销——只有 scalp 亚洲时段硬闸门保留。）
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .regime import compute_market_regime
from .resonance import compute_resonance

GateAction = Literal["ANALYSE", "BLOCKED"]

# 点差峰值达到该值（价格单位）即视为异常扩大，写入标注（剥头皮目标 3-5 个点）。
SPREAD_DOWNGRADE_THRESHOLD = 0.5
# 点差分位软标注阈值：当前点差处于近期历史高位（≥80 分位）时标注成本警示。
# 依据更新（2026-08-07 剥离 8/3 单日伪影后复算 141 单）：全样本口径"点差≥80分位
# -0.58R"剥离后仅剩 2 单，十分位单调性检验非单调（Spearman -0.137）——证据不足
# 以支撑"禁方向"硬否决，降级为软标注（成本机制真实，成本警示保留）。
SPREAD_BLOCK_PERCENTILE = 0.8
# 共振明确阈值：|score| ≥ 0.5 视为方向证据明确；否则标注"共振不明确"。
RESONANCE_CLEAR_THRESHOLD = 0.5
# 共振偏空阈值：score ≤ -0.5 时标注偏空结构期望偏弱（软警示）。
# 依据（2026-08-07 剥离 8/3 伪影后复算）：共振偏空 18 单 avg_r -0.184 vs
# 保留组 +0.337——弱负效应保留为纪律提示；不再引用已证伪的 SHORT 整体对比。
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
    # 点差成本已由 spread_downgrade_reason 软标注给出，此处不再重复报普通警告
    # （避免同一条点差产生两条标注）。
    if spread_downgrade_reason(tick_health) is not None:
        return None
    return None


def spread_downgrade_reason(tick_health: dict[str, object] | None) -> str | None:
    """点差成本软标注：点差处于近期历史高位（≥80 分位）或峰值异常 → 标注成本警示。

    依据更新（2026-08-07 剥离 8/3 单日伪影后复算）：全样本口径"点差≥80分位
    -0.58R"几乎全部来自 8/3 凌晨亚洲 scalp 潮（剥离后仅剩 2 单），且十分位
    单调性检验显示非单调（Spearman -0.137，低分位段 -0.566R 同样差）——"点差
    是最大负期望来源"不成立。点差的成本机制（高成本侵蚀期望）真实存在，但
    证据不支持"禁方向"的硬否决强度：降级为软标注，供模型与交易者权衡成本。
    """
    if not isinstance(tick_health, dict) or tick_health.get("available") is not True:
        return None
    percentile = tick_health.get("spread_percentile")
    if isinstance(percentile, (int, float)) and percentile >= SPREAD_BLOCK_PERCENTILE:
        return f"点差处于近期历史高位（{percentile:.0%} 分位），入场成本偏高，注意止盈止损空间被侵蚀"
    spread_max = tick_health.get("spread_max")
    if isinstance(spread_max, (int, float)) and spread_max >= SPREAD_DOWNGRADE_THRESHOLD:
        return f"点差异常扩大（峰值 {spread_max:.2f}），入场成本偏高，注意止盈止损空间被侵蚀"
    return None


def session_block_reason(snapshot: dict[str, object], mode: str) -> str | None:
    """scalp 模式亚洲时段禁方向：流动性差、点差宽、方向浅且易反转。

    依据（2026-08-07 剥离伪影后复算）：全样本中 scalp+asia 43 单 -0.52R 几乎
    全部来自 8/3 凌晨亚洲 scalp 潮（剥离后仅剩 1 单）——本地证据本质上是
    8/3 单日幸存者规则；但外部共识（pro-scalper/goldscalpers 等多项目）一致：
    剥头皮只在伦敦/重叠/纽约午盘做，亚洲时段只观察。机制（亚洲流动性差+
    点差宽）成立且与 8/3 灾难方向一致（若当时生效可拦 -25R），保留硬闸门，
    依据以外部共识+机制为主、本地伪影为辅。swing 模式保留方向。
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
    """共振明确偏空时标注：本地历史中偏空结构期望偏弱（剥离伪影后的弱化版警示）。

    依据（2026-08-07 剥离 8/3 单日伪影后复算 141 单）：共振偏空 18 单 avg_r
    -0.184 vs 保留组 +0.337——仍有负效应但大幅弱于全样本口径（-0.50R），且
    该效应与 8/3 凌晨亚洲 scalp 潮高度重叠，独立证据有限。保留为"空头需更强
    证据"的纪律提示（军师模式不阻断方向，保留空头自由度），措辞不再引用
    已证伪的 SHORT 整体胜率对比。只消费已收盘 K 线的确定性 score，缺失不标注。
    """
    score = resonance.get("score")
    if not isinstance(score, (int, float)) or score > RESONANCE_BEAR_BREAKEVEN_SCORE:
        return None
    return (
        f"共振明确偏空（score {score:+.2f}）：本地历史中偏空结构期望偏弱"
        "（avg_r -0.18），空头建议需更强的宏观/事件证据与更严的风控"
    )


def short_bias_downgrade_reason(regime: dict[str, object]) -> str | None:
    """强趋势市做空警示：历史数据中"空头整体弱"已被证伪，仅保留趋势维度的提示。

    依据更新（2026-08-07 剥离 8/3 单日伪影后复算）：SHORT 在剥离 8/3 的干净样本
    中 38 单 avg_r +0.27（正期望）——此前"空头 30% vs 多头 55%"的负期望完全来自
    8/3 凌晨亚洲 scalp 潮（28 单 -25.78R）的伪影，方向本身无结构性缺陷。因此撤销
    "空头历史弱"的实证警示，只保留纯趋势维度的风险提示（不引用已证伪的胜率对比）。
    """
    if regime.get("available") is not True:
        return None
    trend_direction = regime.get("trend_direction")
    if trend_direction == "buy":
        # 强趋势多头市做空 = 逆势（纯趋势维度，无空头历史弱期望的背书）
        return (
            "强趋势多头市（双周期 ADX ≥ 25）：逆势做空逆市场主流方向，"
            "除非有极强的宏观/事件证据，追空风险高"
        )
    if trend_direction == "sell":
        # 强趋势空头市做空 = 顺势（趋势维度无警示价值，不再附加空头历史弱）
        return None
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
    expected_symbol: str = "XAUUSD",
) -> GateResult:
    # 品种名比较大小写不敏感（MT5 经纪商符号可能为 XAUUSD.s / xauusd.s 等变体）。
    if (
        snapshot.get("identity_match") is not True
        or str(snapshot.get("symbol") or "").upper() != expected_symbol.upper()
    ):
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
    # 入场纪律硬闸门（2026-08-03 升级）：scalp 亚洲时段 → 禁方向。
    # 分析保留（allow_model=True），只禁方向建议——军师模式不锁死分析。
    # 点差已降级为软标注（2026-08-07 剥离伪影后证据不足支撑硬否决）。
    directional_allowed = True
    for block_reason in (
        session_block_reason(snapshot, mode),
    ):
        if block_reason is not None:
            directional_allowed = False
            warnings.append(block_reason)
    # 收集全部风险标注：EA 风控、tick 健康、点差成本、共振、市场状态、时段流动性。
    for warn in (
        ea_downgrade_reason(ea_status),
        tick_downgrade_reason(tick_health),
        spread_downgrade_reason(tick_health),
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
        captured = datetime.fromisoformat(timestamp).astimezone(timezone.utc)
    except ValueError:
        return float("inf")
    return max(0.0, (now.astimezone(timezone.utc) - captured).total_seconds())


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
