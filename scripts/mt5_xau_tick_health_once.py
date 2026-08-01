"""One-shot read-only XAUUSD tick health probe for the XAU analysis console.

采集最近 window 秒内的 tick 流，输出点差统计与停滞标志，供控制台闸门
判断是否降级（点差异常扩大 / 报价流停滞）。只读行情，不触碰账户与交易。

用法（需 MT5 终端已登录运行）：
    python mt5_xau_tick_health_once.py --symbol XAUUSD --output out.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

WINDOW_SECONDS = 60
STALL_AFTER_SECONDS = 15
MAX_TICKS = 200_000


def emit(payload: dict[str, object], output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read-only XAUUSD tick health probe.")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--output", required=True)
    parser.add_argument("--window-seconds", type=int, default=WINDOW_SECONDS)
    args = parser.parse_args(argv)
    output = Path(args.output)

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return emit(
            {"available": False, "reason": "MetaTrader5 包不可用"}, output
        )

    if not mt5.initialize():
        mt5.shutdown()
        return emit(
            {"available": False, "reason": "MT5 终端连接失败"}, output
        )

    try:
        if not mt5.symbol_select(args.symbol, True):
            return emit(
                {"available": False, "reason": f"无法订阅品种 {args.symbol}"},
                output,
            )
        now_epoch = time.time()
        start_epoch = now_epoch - args.window_seconds
        ticks = mt5.copy_ticks_from(
            args.symbol, start_epoch, MAX_TICKS, mt5.COPY_TICKS_ALL
        )
    finally:
        mt5.shutdown()

    captured_utc = datetime.now(UTC).isoformat()
    if ticks is None or len(ticks) == 0:
        return emit(
            {
                "available": True,
                "symbol": args.symbol,
                "window_seconds": args.window_seconds,
                "ticks": 0,
                "spread_median": None,
                "spread_max": None,
                "last_tick_age_seconds": None,
                "stalled": True,
                "stall_reason": "窗口内无 tick（市场休市或报价中断）",
                "captured_utc": captured_utc,
            },
            output,
        )

    spreads = [float(tick["ask"] - tick["bid"]) for tick in ticks]
    last_tick_epoch = float(ticks[-1]["time"])
    last_tick_age = max(0.0, now_epoch - last_tick_epoch)
    stalled = last_tick_age > STALL_AFTER_SECONDS
    payload: dict[str, object] = {
        "available": True,
        "symbol": args.symbol,
        "window_seconds": args.window_seconds,
        "ticks": int(len(ticks)),
        "spread_median": round(statistics.median(spreads), 5),
        "spread_max": round(max(spreads), 5),
        "last_tick_age_seconds": round(last_tick_age, 1),
        "stalled": stalled,
        "captured_utc": captured_utc,
    }
    if stalled:
        payload["stall_reason"] = f"最近 tick 距今 {last_tick_age:.0f} 秒"
    return emit(payload, output)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
