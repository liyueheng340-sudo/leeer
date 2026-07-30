"""Read a fresh XAUUSD snapshot through the existing read-only MT5 script."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import ConsoleConfig


class SnapshotCaptureError(RuntimeError):
    """The MT5 read-only snapshot command did not return usable facts."""


def capture_snapshot(config: ConsoleConfig, job_id: str) -> dict[str, object]:
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
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
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
