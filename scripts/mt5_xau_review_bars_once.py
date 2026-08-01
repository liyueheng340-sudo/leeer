"""Read-only M5 bar dump for the XAU suggestion review loop.

按 UTC 时间窗拉取 K 线（默认 M5），写入 JSONL，供控制台复盘
历史方向建议（TP/SL 谁先命中）使用。只读行情，不触碰账户与交易。

用法（需 MT5 终端已登录运行）：
    python mt5_xau_review_bars_once.py --symbol XAUUSD \
        --from-utc 2026-07-30T00:00:00+00:00 \
        --to-utc 2026-07-31T00:00:00+00:00 \
        --output review_bars.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

TIMEFRAME_MAP = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
}
MAX_BARS = 20_000


def _parse_utc(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError as error:
        raise SystemExit(f"无效 UTC 时间：{value}") from error


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read-only XAUUSD bar dump for review.")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--from-utc", required=True)
    parser.add_argument("--to-utc", required=True)
    parser.add_argument("--timeframe", default="M5", choices=sorted(TIMEFRAME_MAP))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    start = _parse_utc(args.from_utc)
    end = _parse_utc(args.to_utc)
    if end <= start:
        raise SystemExit("--to-utc 必须晚于 --from-utc")

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print(json.dumps({"error": "MetaTrader5 包不可用"}, ensure_ascii=False))
        return 1

    if not mt5.initialize():
        print(json.dumps({"error": "MT5 终端连接失败"}, ensure_ascii=False))
        mt5.shutdown()
        return 1

    try:
        if not mt5.symbol_select(args.symbol, True):
            print(json.dumps({"error": f"无法订阅品种 {args.symbol}"}, ensure_ascii=False))
            return 1
        timeframe = getattr(mt5, TIMEFRAME_MAP[args.timeframe])
        rates = mt5.copy_rates_range(args.symbol, timeframe, start, end)
    finally:
        mt5.shutdown()

    if rates is None or len(rates) == 0:
        print(json.dumps({"output": str(args.output), "bars": 0}, ensure_ascii=False))
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with args.output.open("w", encoding="utf-8") as handle:
        for bar in rates[-MAX_BARS:]:
            handle.write(
                json.dumps(
                    {
                        "time": int(bar["time"]),
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1
    print(json.dumps({"output": str(args.output), "bars": written}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
