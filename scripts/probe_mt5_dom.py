"""Probe whether the broker provides non-empty Depth of Market for XAUUSD.

P3 验证脚本：OTC 现货金的 DOM 由经纪商单方提供，质量存疑。
先跑这个脚本确认 market_book_get 是否返回非空档位，再决定 DOM 是否
值得进入控制台展示层。空 → NO-GO；非空 → 仅作展示，不进闸门与模型。

用法（需 MT5 终端已登录运行）：
    python scripts/probe_mt5_dom.py --symbol XAUUSD
"""

from __future__ import annotations

import argparse
import json
import sys
import time


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Probe MT5 Depth of Market availability.")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args(argv)

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print(json.dumps({"dom_available": False, "reason": "MetaTrader5 包不可用"}))
        return 1

    if not mt5.initialize():
        print(json.dumps({"dom_available": False, "reason": "MT5 终端连接失败"}))
        mt5.shutdown()
        return 1

    try:
        if not mt5.market_book_add(args.symbol):
            print(
                json.dumps(
                    {
                        "dom_available": False,
                        "symbol": args.symbol,
                        "reason": "market_book_add 失败：经纪商未提供该品种深度",
                    },
                    ensure_ascii=False,
                )
            )
            return 1

        samples: list[dict[str, object]] = []
        for _ in range(max(1, args.samples)):
            book = mt5.market_book_get(args.symbol)
            if book:
                bids = [level for level in book if level.type == 0]
                asks = [level for level in book if level.type == 1]
                samples.append(
                    {
                        "levels_total": len(book),
                        "bid_levels": len(bids),
                        "ask_levels": len(asks),
                        "best_bid_volume": bids[0].volume if bids else None,
                        "best_ask_volume": asks[0].volume if asks else None,
                    }
                )
            else:
                samples.append({"levels_total": 0})
            time.sleep(0.5)

        non_empty = [sample for sample in samples if sample.get("levels_total", 0) > 0]
        verdict = {
            "dom_available": bool(non_empty),
            "symbol": args.symbol,
            "samples": samples,
            "verdict": (
                "经纪商返回了非空深度档位。注意：OTC 现货深度仅为单方报价，"
                "建议仅作展示层，不进闸门与模型。"
                if non_empty
                else "所有采样均为空档位。DOM 对该经纪商 XAUUSD 不可用 → NO-GO。"
            ),
        }
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0 if non_empty else 1
    finally:
        mt5.market_book_release(args.symbol)
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
