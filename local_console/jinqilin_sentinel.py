"""Deterministic single-direction trend risk sentinel for martingale-grid EAs.

为金麒麟类"双向网格马丁"EA 提供外部风险哨兵：基于已收盘 K 线复算的
确定性事实（共振/市场状态/分形位/背离/点差/新闻窗口）评估"当前是否处于
单边行情高风险环境"——马丁网格在单边趋势中是最脆弱的状态。

全部输入来自系统已有确定性事实（resonance/regime/macd_divergence/
tick_health/event_context），无模型推断；风险等级只作预警，不阻断任何分析。
"""

from __future__ import annotations

# 各风险信号权重（0-10 分制，累加后分级）。
RISK_WEIGHTS = {
    "trending_aligned": 3,      # 强趋势市且方向与共振一致 → 单边风险最高
    "trending_only": 2,         # 强趋势市但共振弱
    "volatility_high": 1,       # 波动放大（StdDev 超阈值）
    "iv_high": 1,               # IV Rank 高位（期权市场预期大波动）
    "near_breakout": 1,         # H1 区间位置极端（可能突破）
    "cci_extreme": 1,           # CCI 轨外（价格延伸过度，回归或加速二选一）
    "ema_extended": 1,          # EMA 延伸过度（追价风险）
    "divergence": 1,            # MACD 背离（趋势反转预警）
    "news_window": 1,           # 24h 内高影响事件（波动加剧）
    "spread_high": 1,           # 点差历史高位（补单成本高）
}

# 分级阈值：0-2 低 / 3-4 中 / 5-7 高 / 8+ 极高。
LEVEL_LOW_MAX = 2
LEVEL_MEDIUM_MAX = 4
LEVEL_HIGH_MAX = 7

# 区间位置极端判定（H1 range_location_8 接近 0=区间底 1=区间顶）。
BREAKOUT_LOCATION_MIN = 0.15
BREAKOUT_LOCATION_MAX = 0.85
# 新闻窗口：未来高影响事件距现在少于该小时数 → 风险分。
NEWS_WINDOW_HOURS = 24
# IV Rank 高位阈值。
IV_RANK_HIGH = 0.8
# 延伸度阈值（与 guard.EMA_EXTENSION_WARN_ATR 一致）。
EMA_EXTENDED_ATR = 2.5


def _trending_risk(regime: dict[str, object], resonance: dict[str, object]) -> str | None:
    """强趋势市且共振方向一致 → 'trending_aligned'；仅强趋势 → 'trending_only'。"""
    if regime.get("available") is not True or regime.get("regime") != "trending":
        return None
    trend_direction = regime.get("trend_direction")
    score = resonance.get("score")
    if isinstance(trend_direction, str) and isinstance(score, (int, float)):
        aligned = (trend_direction == "buy" and score >= 0.5) or (
            trend_direction == "sell" and score <= -0.5
        )
        if aligned:
            return "trending_aligned"
    return "trending_only"


def _breakout_location(snapshot: dict[str, object]) -> bool:
    """H1（兜底 M15）区间位置极端 → 可能突破当前区间。"""
    structure = snapshot.get("timeframe_structure")
    if not isinstance(structure, dict):
        return False
    for timeframe in ("h1", "m15"):
        frame = structure.get(timeframe)
        if isinstance(frame, dict) and isinstance(frame.get("range_location_8"), (int, float)):
            location = float(frame["range_location_8"])
            return location <= BREAKOUT_LOCATION_MIN or location >= BREAKOUT_LOCATION_MAX
    return False


def _news_window(event_context: dict[str, object], now_utc) -> bool:
    """24 小时内有高影响事件 → 波动加剧风险。"""
    if not isinstance(event_context, dict) or event_context.get("status") != "verified_clear":
        return False
    next_event = event_context.get("next_event")
    if not isinstance(next_event, dict):
        return False
    event_utc = next_event.get("utc")
    if not isinstance(event_utc, str):
        return False
    try:
        from datetime import datetime

        event_time = datetime.fromisoformat(event_utc)
    except ValueError:
        return False
    hours = (event_time - now_utc).total_seconds() / 3600.0
    return 0 <= hours <= NEWS_WINDOW_HOURS


def compute_jinqilin_sentinel(
    snapshot: dict[str, object],
    *,
    resonance: dict[str, object] | None = None,
    regime: dict[str, object] | None = None,
    tick_health: dict[str, object] | None = None,
    event_context: dict[str, object] | None = None,
    iv_context: dict[str, object] | None = None,
    now_utc=None,
) -> dict[str, object]:
    """评估金麒麟类马丁网格 EA 当前所处的单边行情风险。

    返回结构（available=True 时）：
        risk_level    LOW / MEDIUM / HIGH / CRITICAL
        risk_score    0-10 累加风险分
        flags         命中的风险信号名（含权重说明）
        advice        给交易者的可执行建议（中文，一句）
    """
    from datetime import datetime, timezone

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    if resonance is None:
        resonance = snapshot.get("timeframe_resonance")
    if regime is None:
        regime = snapshot.get("market_regime")
    if not isinstance(resonance, dict) or resonance.get("available") is not True:
        resonance = {}
    if not isinstance(regime, dict) or regime.get("available") is not True:
        regime = {}

    flags: list[str] = []
    score = 0

    trending = _trending_risk(regime, resonance)
    if trending == "trending_aligned":
        flags.append("强趋势市且共振同向（单边行情高危）")
        score += RISK_WEIGHTS["trending_aligned"]
    elif trending == "trending_only":
        flags.append("强趋势市（ADX 双周期 ≥25）")
        score += RISK_WEIGHTS["trending_only"]

    if regime.get("volatility_confirmed") is True:
        flags.append("波动放大（StdDev 超阈值）")
        score += RISK_WEIGHTS["volatility_high"]

    if iv_context and isinstance(iv_context.get("iv_rank"), (int, float)) and float(iv_context["iv_rank"]) >= IV_RANK_HIGH:
        flags.append(f"IV Rank {float(iv_context['iv_rank']):.0%}（期权市场预期大波动）")
        score += RISK_WEIGHTS["iv_high"]

    if _breakout_location(snapshot):
        flags.append("H1 价格位于区间边缘（突破概率上升）")
        score += RISK_WEIGHTS["near_breakout"]

    if isinstance(regime.get("cci_extreme"), dict):
        flags.append("CCI 轨外（价格延伸过度）")
        score += RISK_WEIGHTS["cci_extreme"]

    extension = regime.get("ema_extension")
    if (
        isinstance(extension, dict)
        and isinstance(extension.get("atr_distance"), (int, float))
        and float(extension["atr_distance"]) >= EMA_EXTENDED_ATR
    ):
        flags.append(f"价格延伸 {float(extension['atr_distance']):g}×ATR（禁追价）")
        score += RISK_WEIGHTS["ema_extended"]

    divergence = snapshot.get("macd_divergence")
    if isinstance(divergence, dict) and divergence.get("any_divergence") is True:
        flags.append("MACD 背离出现（趋势反转预警）")
        score += RISK_WEIGHTS["divergence"]

    if _news_window(event_context or {}, now_utc):
        flags.append("24 小时内高影响事件（波动加剧）")
        score += RISK_WEIGHTS["news_window"]

    if (
        isinstance(tick_health, dict)
        and isinstance(tick_health.get("spread_percentile"), (int, float))
        and float(tick_health["spread_percentile"]) >= 0.8
    ):
        flags.append("点差历史高位（补单成本高）")
        score += RISK_WEIGHTS["spread_high"]

    if score <= LEVEL_LOW_MAX:
        risk_level = "LOW"
        advice = "当前环境对网格马丁相对友好，正常按参数运行"
    elif score <= LEVEL_MEDIUM_MAX:
        risk_level = "MEDIUM"
        advice = "存在部分单边信号，建议留意持仓深度并适当收紧风控"
    elif score <= LEVEL_HIGH_MAX:
        risk_level = "HIGH"
        advice = "单边行情风险高：建议人工暂停金麒麟或缩减仓位，等待环境回归震荡"
    else:
        risk_level = "CRITICAL"
        advice = "极高单边风险：强烈建议立即暂停金麒麟，检查持仓深度与浮亏后再评估"

    return {
        "available": True,
        "risk_level": risk_level,
        "risk_score": score,
        "flags": flags,
        "advice": advice,
        "note": "确定性事实：由共振/市场状态/背离/点差/新闻窗口复算的单边行情风险，非模型推断。",
    }
