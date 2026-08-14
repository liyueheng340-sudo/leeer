"""One-shot combined XAUUSD capture: market context + tick health, one MT5 session.

此前控制台每个任务要起两个 MT5 子进程（快照脚本 + tick 探针），各自
initialize/shutdown 一次，是最主要的重复调用。本脚本在同一 MT5 会话内
完成两份采集，输出到同一 JSONL 文件的两条带标记记录：

    {"record": "market_context", ...}
    {"record": "tick_health", ...}

行情快照本体复用仓库内只读脚本的 build_market_context（通过 --context-script
指向 scripts/mt5_xau_market_context_once.py 动态加载），不复制其逻辑。

用法（需 MT5 终端已登录运行）：
    python mt5_xau_snapshot_with_ticks_once.py \
        --symbol XAUUSD --output out.jsonl \
        --context-script scripts\\mt5_xau_market_context_once.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

WINDOW_SECONDS = 60
STALL_AFTER_SECONDS = 15
MAX_TICKS = 200_000


def load_context_module(path: Path) -> ModuleType:
    """Load the external read-only snapshot module from its file path."""
    spec = importlib.util.spec_from_file_location("mt5_xau_market_context_once", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载快照脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_tick_health(mt5, symbol: str, window_seconds: int) -> dict[str, object]:
    now_epoch = time.time()
    ticks = mt5.copy_ticks_from(symbol, now_epoch - window_seconds, MAX_TICKS, mt5.COPY_TICKS_ALL)
    captured_utc = datetime.now(UTC).isoformat()
    base: dict[str, object] = {
        "available": True,
        "symbol": symbol,
        "window_seconds": window_seconds,
        "captured_utc": captured_utc,
    }
    if ticks is None or len(ticks) == 0:
        return {
            **base,
            "ticks": 0,
            "spread_median": None,
            "spread_max": None,
            "last_tick_age_seconds": None,
            "stalled": True,
            "stall_reason": "窗口内无 tick（市场休市或报价中断）",
        }
    spreads = [float(tick["ask"] - tick["bid"]) for tick in ticks]
    last_tick_age = max(0.0, now_epoch - float(ticks[-1]["time"]))
    payload: dict[str, object] = {
        **base,
        "ticks": int(len(ticks)),
        "spread_median": round(statistics.median(spreads), 5),
        "spread_max": round(max(spreads), 5),
        "last_tick_age_seconds": round(last_tick_age, 1),
        "stalled": last_tick_age > STALL_AFTER_SECONDS,
    }
    if payload["stalled"]:
        payload["stall_reason"] = f"最近 tick 距今 {last_tick_age:.0f} 秒"
    return payload


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Combined read-only XAUUSD snapshot + tick capture.")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--context-script", required=True, type=Path)
    parser.add_argument("--tick-window-seconds", type=int, default=WINDOW_SECONDS)
    args = parser.parse_args(argv)

    context_module = load_context_module(args.context_script)
    mt5 = context_module.import_mt5()
    if not mt5.initialize():
        print(json.dumps({"record": "error", "reason": "mt5.initialize 失败"}, ensure_ascii=False))
        return 1
    try:
        symbol = context_module.resolve_symbol(mt5, args.symbol)
        mt5.symbol_select(symbol, True)
        context = context_module.build_market_context(mt5, symbol)
        tick_health = collect_tick_health(mt5, symbol, args.tick_window_seconds)
    finally:
        mt5.shutdown()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"record": "market_context", **context}, ensure_ascii=False) + "\n")
        handle.write(json.dumps({"record": "tick_health", **tick_health}, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "identity_match": context.get("identity_match"),
                "ticks": tick_health.get("ticks"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
