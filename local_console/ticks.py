"""Tick-level market sensor feeding the XAU fact gate (read-only).

封装 scripts/mt5_xau_tick_health_once.py：采集近 60 秒 tick 流的点差统计
与停滞标志。传感器失败只返回 available=False，绝不阻断任务——快照本身
仍是第一事实来源，tick 流只是闸门的辅助降级触发器。
"""

from __future__ import annotations

import json
import subprocess

from .config import ConsoleConfig

TICK_PROBE_TIMEOUT_SECONDS = 30


def _unavailable(reason: str) -> dict[str, object]:
    return {"available": False, "reason": reason}


def capture_tick_health(config: ConsoleConfig, job_id: str) -> dict[str, object]:
    """Run the tick probe and parse its JSONL output; never raises."""
    if not config.mt5_python.is_file():
        return _unavailable("MT5 Python 解释器不可用")
    if not config.tick_script_path.is_file():
        return _unavailable("tick 采集脚本缺失")
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    output = config.snapshots_dir / f"{job_id}_ticks.jsonl"
    command = [
        str(config.mt5_python),
        str(config.tick_script_path),
        "--symbol",
        "XAUUSD",
        "--output",
        str(output),
    ]
    try:
        subprocess.run(
            command, check=True, capture_output=True, text=True,
            timeout=TICK_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return _unavailable(f"tick 采集失败：{error}")
    try:
        line = output.read_text(encoding="utf-8").strip().splitlines()[-1]
        payload = json.loads(line)
    except (IndexError, OSError, json.JSONDecodeError):
        return _unavailable("tick 采集输出无效")
    if not isinstance(payload, dict) or "available" not in payload:
        return _unavailable("tick 采集输出缺少状态字段")
    return payload
