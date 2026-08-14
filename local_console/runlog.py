"""Structured run log for the XAU console (JSONL, append-only).

每个任务阶段转换、闸门判定、复盘结果都落一行，回答三个问题：
哪里慢（stage 间隔）、哪里常失败（stage=FAILED 分布）、
传感器/降级有多频繁（gate action 分布）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_event(path: Path, **fields: Any) -> None:
    """Append one JSON line; never raises — logging must not break jobs."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
