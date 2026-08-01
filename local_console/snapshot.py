"""Read fresh XAUUSD facts through read-only MT5 scripts.

两种采集路径：
- capture_combined：单个子进程、单个 MT5 会话同时产出快照与 tick 健康
  （scripts/mt5_xau_snapshot_with_ticks_once.py），是默认路径，消除重复
  MT5 initialize。
- capture_snapshot：旧的外部快照脚本，仅作为合并采集失败时的回退。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import ConsoleConfig


class SnapshotCaptureError(RuntimeError):
    """The MT5 read-only snapshot command did not return usable facts."""


SNAPSHOT_TIMEOUT_SECONDS = 45


def _parse_tagged_records(output: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Parse combined JSONL output into (market_context, tick_health)."""
    context: dict[str, object] | None = None
    tick_health: dict[str, object] | None = None
    try:
        lines = output.read_text(encoding="utf-8").strip().splitlines()
    except OSError as error:
        raise SnapshotCaptureError("MT5 snapshot output is invalid") from error
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        tag = row.pop("record", None)
        if tag == "market_context":
            context = row
        elif tag == "tick_health":
            tick_health = row
    if context is None or tick_health is None:
        raise SnapshotCaptureError("合并采集输出缺少 market_context 或 tick_health 记录")
    return context, tick_health


def capture_combined(
    config: ConsoleConfig, job_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    """One subprocess, one MT5 session: market snapshot + tick health."""
    if not config.mt5_python.is_file():
        raise SnapshotCaptureError(f"MT5 Python interpreter is unavailable: {config.mt5_python}")
    if not config.combined_script_path.is_file():
        raise SnapshotCaptureError(f"合并采集脚本缺失：{config.combined_script_path}")
    if not config.mt5_snapshot_script.is_file():
        raise SnapshotCaptureError(f"MT5 snapshot script is unavailable: {config.mt5_snapshot_script}")
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    output = config.snapshots_dir / f"{job_id}.jsonl"
    command = [
        str(config.mt5_python),
        str(config.combined_script_path),
        "--symbol",
        "XAUUSD",
        "--output",
        str(output),
        "--context-script",
        str(config.mt5_snapshot_script),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=SNAPSHOT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise SnapshotCaptureError(str(error)) from error
    return _parse_tagged_records(output)


def capture_snapshot(config: ConsoleConfig, job_id: str) -> dict[str, object]:
    """Legacy single-purpose capture via the external snapshot script (fallback)."""
    if not config.mt5_python.is_file():
        raise SnapshotCaptureError(f"MT5 Python interpreter is unavailable: {config.mt5_python}")
    if not config.mt5_snapshot_script.is_file():
        raise SnapshotCaptureError(f"MT5 snapshot script is unavailable: {config.mt5_snapshot_script}")
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    output = config.snapshots_dir / f"{job_id}.jsonl"
    command = [
        str(config.mt5_python),
        str(config.mt5_snapshot_script),
        "--symbol",
        "XAUUSD",
        "--output",
        str(output),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=SNAPSHOT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise SnapshotCaptureError(str(error)) from error
    try:
        line = output.read_text(encoding="utf-8").strip().splitlines()[-1]
        payload = json.loads(line)
    except (IndexError, OSError, json.JSONDecodeError) as error:
        raise SnapshotCaptureError("MT5 snapshot output is invalid") from error
    if not isinstance(payload, dict):
        raise SnapshotCaptureError("MT5 snapshot output is not an object")
    return payload
