"""Read-only XAU market-context snapshot for the AI shadow trader."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.mt5_xau_readonly_utils import (
    adx,
    atr,
    cci,
    ema,
    import_mt5,
    macd_series,
    normalize_rates,
    resolve_symbol,
    rsi,
    stddev,
)
from scripts.mt5_xau_time_utils import (
    corrected_mt5_utc_iso,
    infer_mt5_server_offset_seconds,
    raw_mt5_utc_iso,
    session_label_from_corrected_utc,
)

DEFAULT_SYMBOL = "XAUUSD"
DEFAULT_OUTPUT = Path("artifacts/mt5_xau_demo_autotrader/market_context_snapshot.jsonl")


def trade_mode_name(value: Any) -> str:
    return {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(value, "UNKNOWN")


def latest_bar_summary(bars: list[dict[str, float]], *, offset_seconds: int) -> dict[str, Any] | None:
    if not bars:
        return None
    bar = bars[-1]
    return {
        "time": int(bar["time"]),
        "server_labeled_utc": raw_mt5_utc_iso(int(bar["time"])),
        "utc": corrected_mt5_utc_iso(int(bar["time"]), offset_seconds),
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": float(bar["close"]),
        "range": float(bar["high"]) - float(bar["low"]),
    }


def read_rates(mt5: Any, symbol: str, timeframe: int, count: int) -> list[dict[str, float]]:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, count)
    return [] if rates is None else normalize_rates(rates)


def timeframe_structure(bars: list[dict[str, float]], *, offset_seconds: int) -> dict[str, Any] | None:
    if len(bars) < 9:
        return None
    latest = bars[-1]
    recent = bars[-8:]
    previous = bars[-9:-1]
    recent_high = max(bar["high"] for bar in recent)
    recent_low = min(bar["low"] for bar in recent)
    recent_range = recent_high - recent_low
    previous_high = max(bar["high"] for bar in previous)
    previous_low = min(bar["low"] for bar in previous)
    close = float(latest["close"])
    return {
        "anchor_time": int(latest["time"]),
        "anchor_utc": corrected_mt5_utc_iso(int(latest["time"]), offset_seconds),
        "open": float(latest["open"]),
        "high": float(latest["high"]),
        "low": float(latest["low"]),
        "close": close,
        "body_direction": "buy" if close > latest["open"] else "sell" if close < latest["open"] else "neutral",
        "change_1": round(close - float(bars[-2]["close"]), 6),
        "change_4": round(close - float(bars[-5]["close"]), 6),
        "atr_14": round(atr(bars, 14), 6) if len(bars) >= 15 else None,
        # 趋势强度三要素（EA 精华：ADX 双周期过滤 / StdDev 波动确认 / RSI 超买超卖）
        "adx_14": round(adx(bars, 14), 6) if len(bars) >= 30 else None,
        "rsi_14": round(rsi(bars, 14), 6) if len(bars) >= 16 else None,
        "stddev_20": round(stddev(bars, 20), 6) if len(bars) >= 20 else None,
        # EA 精华扩展（2026-08-06）：EMA 延伸度（king-v2 PriceNotExtended 防追价）、
        # CCI 过滤（恒鑫 EA 精华：±100 上下轨）、MACD 三要素（背离检测输入）。
        "ema_20": round(ema(bars, 20), 6) if len(bars) >= 20 else None,
        "cci_14": round(cci(bars, 14), 6) if len(bars) >= 14 else None,
        "macd_histogram": macd_series(bars).get("histogram") if len(bars) >= 35 else None,
        "range_8": round(recent_range, 6),
        "range_location_8": round((close - recent_low) / recent_range, 6) if recent_range > 0 else 0.5,
        "breakout_up": close > previous_high,
        "breakout_down": close < previous_low,
    }


def build_market_context(
    mt5: Any,
    symbol: str,
    *,
    expected_login: int | None = None,
    expected_server: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    capture_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    account = mt5.account_info()
    terminal = mt5.terminal_info()
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    m1 = read_rates(mt5, symbol, mt5.TIMEFRAME_M1, 60)
    m5 = read_rates(mt5, symbol, mt5.TIMEFRAME_M5, 80)
    m15 = read_rates(mt5, symbol, mt5.TIMEFRAME_M15, 120)
    h1 = read_rates(mt5, symbol, mt5.TIMEFRAME_H1, 120)
    h4 = read_rates(mt5, symbol, mt5.TIMEFRAME_H4, 80)
    d1 = read_rates(mt5, symbol, mt5.TIMEFRAME_D1, 60)
    raw_tick_time = getattr(tick, "time", None) if tick is not None else None
    offset_seconds = infer_mt5_server_offset_seconds(capture_utc=capture_utc, raw_tick_time=raw_tick_time)

    login = getattr(account, "login", None)
    server = getattr(account, "server", None)
    identity_match = True
    if expected_login is not None and int(login) != int(expected_login):
        identity_match = False
    if expected_server and str(server) != expected_server:
        identity_match = False

    context = {
        "timestamp": capture_utc.isoformat(),
        "scope": "xau_ai_shadow_market_context",
        "server": server,
        "company": getattr(terminal, "company", None),
        "terminal_build": getattr(terminal, "build", None),
        "expected_server": expected_server,
        "identity_match": identity_match,
        "market_state_flags": [
            *([] if identity_match else ["BROKER_FACT_MISMATCH"]),
        ],
        "account_trade_reads": "FORBIDDEN",
        "account_trade_writes": "FORBIDDEN",
        "symbol": symbol,
        "bid": float(tick.bid) if tick is not None else None,
        "ask": float(tick.ask) if tick is not None else None,
        "spread": round(float(tick.ask) - float(tick.bid), 6) if tick is not None else None,
        "tick_time": raw_tick_time,
        "tick_server_labeled_utc": raw_mt5_utc_iso(raw_tick_time) if tick is not None else None,
        "tick_utc": corrected_mt5_utc_iso(raw_tick_time, offset_seconds) if tick is not None else None,
        "digits": getattr(info, "digits", None),
        "point": getattr(info, "point", None),
        "trade_stops_level": getattr(info, "trade_stops_level", None),
        "trade_freeze_level": getattr(info, "trade_freeze_level", None),
        "swap_facts": {
            "swap_mode": getattr(info, "swap_mode", None),
            "swap_long": getattr(info, "swap_long", None),
            "swap_short": getattr(info, "swap_short", None),
            "triple_rollover_day": getattr(info, "swap_rollover3days", None),
            "trade_contract_size": getattr(info, "trade_contract_size", None),
            "point": getattr(info, "point", None),
        },
        "time_semantics": {
            "server_offset_seconds": offset_seconds,
            "server_offset_hours": round(offset_seconds / 3600.0, 3),
            "capture_basis": "utc",
            "market_data_basis": "mt5_server_time_normalized_to_utc",
        },
        "latest_closed_bars": {
            "m1": latest_bar_summary(m1, offset_seconds=offset_seconds),
            "m5": latest_bar_summary(m5, offset_seconds=offset_seconds),
            "m15": latest_bar_summary(m15, offset_seconds=offset_seconds),
            "h1": latest_bar_summary(h1, offset_seconds=offset_seconds),
            "h4": latest_bar_summary(h4, offset_seconds=offset_seconds),
        },
        "timeframe_structure": {
            "m5": timeframe_structure(m5, offset_seconds=offset_seconds),
            "m15": timeframe_structure(m15, offset_seconds=offset_seconds),
            "h1": timeframe_structure(h1, offset_seconds=offset_seconds),
            "h4": timeframe_structure(h4, offset_seconds=offset_seconds),
        },
        # EA 精华序列（2026-08-06）：D1 收盘 K 线（Gold Trade Pro 分形突破位）、
        # M5/M15/H1 紧凑 bar 序列（MACD 背离检测输入）。
        "d1_bars": [
            {
                "time": int(bar["time"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
            }
            for bar in d1[-40:]
        ],
        "bar_series": {
            tf: [
                {
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                }
                for bar in bars[-60:]
            ]
            for tf, bars in (("m5", m5), ("m15", m15), ("h1", h1))
        },
        "range_m1_5": max((bar["high"] for bar in m1[-5:]), default=0.0) - min((bar["low"] for bar in m1[-5:]), default=0.0),
        "range_m5_5": max((bar["high"] for bar in m5[-5:]), default=0.0) - min((bar["low"] for bar in m5[-5:]), default=0.0),
        "atr_m15": atr(m15, 21) if len(m15) >= 22 else None,
        "session_label": session_label_from_corrected_utc(int(m15[-1]["time"]), offset_seconds) if m15 else None,
    }
    return context


def write_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only XAU market context snapshot.")
    parser.add_argument("--symbol", default=os.environ.get("MT5_SYMBOL", DEFAULT_SYMBOL))
    parser.add_argument("--expected-login", type=int, default=os.environ.get("MT5_EXPECTED_LOGIN"))
    parser.add_argument("--expected-server", default=os.environ.get("MT5_EXPECTED_SERVER"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    mt5 = import_mt5()
    if not mt5.initialize():
        raise SystemExit(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        symbol = resolve_symbol(mt5, args.symbol)
        mt5.symbol_select(symbol, True)
        context = build_market_context(
            mt5,
            symbol,
            expected_login=args.expected_login,
            expected_server=args.expected_server,
        )
        write_jsonl(args.output, context)
        print(json.dumps({"output": str(args.output), "identity_match": context["identity_match"]}, ensure_ascii=False))
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
