"""Compose the gate payload and model facts from captured context.

共振/市场状态只算一次：紧凑版随 gate 落盘（供复盘按情境聚合），
完整版喂给模型，同时复用进 gate 的风险标注，避免每任务重复计算。
"""

from __future__ import annotations

from datetime import UTC, datetime

from .brief import PROMPT_VERSION
from .fractal_levels import compute_fractal_levels
from .guard import GateResult, evaluate_gate
from .jinqilin_sentinel import compute_jinqilin_sentinel
from .macd_divergence import compute_macd_divergence
from .regime import compute_market_regime
from .resonance import compute_resonance, compute_signal_votes
from .snapshot_facts import _key_levels


def build_gate_payload(
    *,
    snapshot: dict[str, object],
    tick_health: dict[str, object],
    ea_status: dict[str, object],
    macro: dict[str, object],
    news: dict[str, object],
    event_context: dict[str, object],
    iv_context: dict[str, object] | None = None,
    mode: str = "scalp",
) -> tuple[GateResult, dict[str, object], dict[str, object], dict[str, object]]:
    """Compute gate + resonance + regime once, return (gate, gate_payload, resonance, regime)."""
    resonance = compute_resonance(snapshot)
    regime = compute_market_regime(snapshot)
    gate = evaluate_gate(
        snapshot,
        event_context,
        datetime.now(UTC),
        tick_health,
        ea_status,
        resonance=resonance,
        regime=regime,
        mode=mode,
    )
    gate_payload = {
        **gate.to_dict(),
        "tick_health": tick_health,
        # EA 风控态只落风险机制字段（不含持仓/盈亏），供前端与复盘追溯降级来源。
        "ea_status": (
            {
                "status": ea_status.get("status"),
                "regime_blocked": ea_status.get("regime_blocked"),
                "hour_blocked": ea_status.get("hour_blocked"),
                "feed": ea_status.get("feed"),
                "age_seconds": ea_status.get("age_seconds"),
            }
            if ea_status.get("available") is True
            else None
        ),
        "macro_status": macro.get("status"),
        "macro_summary": (
            {
                sid: {
                    "label": item.get("label"),
                    "latest": item.get("latest"),
                    "change_recent": item.get("change_recent"),
                    "date": item.get("date"),
                }
                for sid, item in macro.get("series", {}).items()
            }
            if macro.get("status") == "ok"
            else None
        ),
        "event_context": event_context,
        "prompt_version": PROMPT_VERSION,
        "news_status": news.get("status"),
        "news_summary": (
            {
                "count": len(news.get("items", [])),
                "items": [
                    {
                        "title": item.get("title"),
                        "topic": item.get("topic"),
                        "publisher": item.get("publisher"),
                        "utc": item.get("utc"),
                    }
                    for item in news.get("items", [])[:5]
                    if isinstance(item, dict)
                ],
            }
            if news.get("status") == "ok"
            else None
        ),
        "resonance": {
            "available": resonance.get("available"),
            "score": resonance.get("score"),
            "label": resonance.get("label"),
        },
        "regime": {
            "available": regime.get("available"),
            "regime": regime.get("regime"),
            "label": regime.get("label"),
            "trend_direction": regime.get("trend_direction"),
            "rsi_extreme": regime.get("rsi_extreme"),
            "cci_extreme": regime.get("cci_extreme"),
            "ema_extension": regime.get("ema_extension"),
            "volatility_confirmed": regime.get("volatility_confirmed"),
        },
        "fractal_levels": _fractal_summary(snapshot),
        "signal_votes": _signal_summary(snapshot),
        "macd_divergence": _divergence_summary(snapshot),
        "jinqilin_sentinel": _sentinel_summary(
            snapshot, tick_health, event_context, iv_context, resonance, regime
        ),
        "iv": _iv_summary(iv_context),
    }
    return gate, gate_payload, resonance, regime


def _sentinel_summary(
    snapshot: dict[str, object],
    tick_health: dict[str, object],
    event_context: dict[str, object],
    iv_context: dict[str, object] | None,
    resonance: dict[str, object],
    regime: dict[str, object],
) -> dict[str, object] | None:
    """金麒麟单边行情哨兵前端摘要：风险等级 / 分数 / 命中信号 / 建议。"""
    sentinel = compute_jinqilin_sentinel(
        snapshot,
        resonance=resonance,
        regime=regime,
        tick_health=tick_health,
        event_context=event_context,
        iv_context=iv_context,
    )
    if sentinel.get("available") is not True:
        return None
    return {
        "available": True,
        "risk_level": sentinel.get("risk_level"),
        "risk_score": sentinel.get("risk_score"),
        "flags": sentinel.get("flags"),
        "advice": sentinel.get("advice"),
    }


def _fractal_summary(snapshot: dict[str, object]) -> dict[str, object] | None:
    """分形突破位前端摘要：只落展示所需字段（最近买/卖突破位 + ATR）。"""
    levels = compute_fractal_levels(snapshot)
    if levels.get("available") is not True:
        return None
    return {
        "available": True,
        "nearest_buy": levels.get("nearest_buy"),
        "nearest_sell": levels.get("nearest_sell"),
        "buy_levels": levels.get("buy_levels"),
        "sell_levels": levels.get("sell_levels"),
        "reference_atr": levels.get("reference_atr"),
    }


def _signal_summary(snapshot: dict[str, object]) -> dict[str, object] | None:
    """多策略投票前端摘要：只落共识与各信号票。"""
    votes = compute_signal_votes(snapshot)
    if votes.get("available") is not True:
        return None
    return {
        "available": True,
        "consensus": votes.get("consensus"),
        "label": votes.get("label"),
        "signals": votes.get("signals"),
    }


def _divergence_summary(snapshot: dict[str, object]) -> dict[str, object] | None:
    """MACD 背离前端摘要：只落各框架背离标记。"""
    divergence = compute_macd_divergence(snapshot)
    if divergence.get("available") is not True:
        return None
    return {
        "available": True,
        "any_divergence": divergence.get("any_divergence"),
        "divergences": {
            tf: {
                "side": entry.get("side"),
                "label": entry.get("label"),
            }
            if isinstance(entry, dict)
            else None
            for tf, entry in divergence.get("divergences", {}).items()
        },
    }


def _iv_summary(iv_context: dict[str, object] | None) -> dict[str, object] | None:
    """IV 前端摘要：只落展示所需字段（ATM IV / 环境 / 偏斜 / Rank / 期限结构）。"""
    if not isinstance(iv_context, dict) or iv_context.get("status") != "ok":
        return None
    return {
        "atm_iv": iv_context.get("atm_iv"),
        "iv_vs_hv": iv_context.get("iv_vs_hv"),
        "skew": iv_context.get("skew"),
        "iv_rank": iv_context.get("iv_rank"),
        "term_slope": iv_context.get("term_slope"),
        "hv20": iv_context.get("hv20"),
        "expiry": iv_context.get("expiry"),
    }


def build_facts(
    snapshot: dict[str, object],
    *,
    macro: dict[str, object],
    tick_health: dict[str, object],
    event_context: dict[str, object],
    resonance: dict[str, object],
    regime: dict[str, object],
    news: dict[str, object],
    iv_context: dict[str, object] | None = None,
) -> dict[str, object]:
    """模型事实包 = MT5 快照 + 宏观背景层 + tick 传感器读数 + 事件日历 + IV 波动层。"""
    facts = dict(snapshot)
    # 关键价位层注入 facts 顶层：source 白名单允许引用 key_levels，
    # 但此前快照字典里没有该键 → 模型引用即被判"不在已提供事实中"被拒
    # （2026-08-03 实测 2 次 REJECTED 根因之一）。注入后 evidence_fields
    # 引用 key_levels 可解析，且 prompt 的 facts_paths 清单也会带上它。
    facts["key_levels"] = _key_levels(snapshot)
    facts["background_macro"] = macro
    facts["tick_health"] = tick_health
    facts["event_context"] = event_context
    facts["timeframe_resonance"] = resonance
    facts["market_regime"] = regime
    facts["news_context"] = news
    # EA 精华新事实（2026-08-06）：分形突破位 / 多策略投票 / MACD 背离。
    fractal = compute_fractal_levels(snapshot)
    if fractal.get("available") is True:
        facts["fractal_levels"] = fractal
    signal_votes = compute_signal_votes(snapshot)
    if signal_votes.get("available") is True:
        facts["signal_votes"] = signal_votes
    divergence = compute_macd_divergence(snapshot)
    if divergence.get("available") is True:
        facts["macd_divergence"] = divergence
    sentinel = compute_jinqilin_sentinel(
        snapshot,
        resonance=resonance,
        regime=regime,
        tick_health=tick_health,
        event_context=event_context,
        iv_context=iv_context,
    )
    if sentinel.get("available") is True:
        facts["jinqilin_sentinel"] = sentinel
    if iv_context is not None:
        facts["option_iv"] = iv_context
    return facts
