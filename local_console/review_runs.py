"""Fetch historical M5 bars from MT5 for the review measurement window."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime

from .config import ConsoleConfig

REVIEW_TIMEFRAME = "M5"
REVIEW_BARS_TIMEOUT_SECONDS = 45


def fetch_review_bars(
    config: ConsoleConfig, start: datetime, end: datetime, tag: str = "review"
) -> list[dict[str, float]]:
    """Pull M5 bars for the review window via the read-only MT5 script."""
    if not config.mt5_python.is_file() or not config.review_script_path.is_file():
        return []
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    output = config.snapshots_dir / f"{tag}_bars.jsonl"
    command = [
        str(config.mt5_python),
        str(config.review_script_path),
        "--symbol",
        config.symbol,
        "--from-utc",
        start.isoformat(),
        "--to-utc",
        end.isoformat(),
        "--output",
        str(output),
    ]
    try:
        subprocess.run(
            command, check=True, capture_output=True, text=True,
            timeout=REVIEW_BARS_TIMEOUT_SECONDS,
        )
        bars: list[dict[str, float]] = []
        for line in output.read_text(encoding="utf-8").strip().splitlines():
            row = json.loads(line)
            if isinstance(row, dict) and {"time", "high", "low", "close"} <= set(row):
                bars.append(row)
        return bars
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
