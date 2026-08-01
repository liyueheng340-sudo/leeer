from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_console.config import ConsoleConfig
from local_console.ticks import capture_tick_health


def make_config(root: Path) -> ConsoleConfig:
    return ConsoleConfig(
        repo_root=root,
        state_dir=root / "runtime",
        mt5_python=root / "python.exe",
        mt5_snapshot_script=root / "snapshot.py",
        backend_url=None,
        quick_model="quick",
        deep_model="deep",
    )


def tick_payload() -> dict[str, object]:
    return {
        "available": True,
        "symbol": "XAUUSD",
        "window_seconds": 60,
        "ticks": 512,
        "spread_median": 0.1,
        "spread_max": 0.18,
        "stalled": False,
        "captured_utc": "2026-07-31T00:00:00+00:00",
    }


class TickHealthTests(unittest.TestCase):
    def test_missing_interpreter_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            (Path(directory) / "scripts").mkdir()
            (Path(directory) / "scripts" / "mt5_xau_tick_health_once.py").write_text("#", encoding="utf-8")

            result = capture_tick_health(config, "job1")

        self.assertEqual(False, result["available"])
        self.assertIn("解释器", result["reason"])

    def test_missing_script_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "python.exe").write_text("#", encoding="utf-8")
            config = make_config(root)

            result = capture_tick_health(config, "job1")

        self.assertEqual(False, result["available"])
        self.assertIn("脚本", result["reason"])

    def test_successful_probe_parses_last_jsonl_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "python.exe").write_text("#", encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts" / "mt5_xau_tick_health_once.py").write_text("#", encoding="utf-8")
            config = make_config(root)

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output") + 1])
                output.write_text(json.dumps(tick_payload()) + "\n", encoding="utf-8")

            with patch("local_console.ticks.subprocess.run", side_effect=fake_run):
                result = capture_tick_health(config, "job1")

        self.assertEqual(True, result["available"])
        self.assertEqual(512, result["ticks"])
        self.assertEqual(0.18, result["spread_max"])
        self.assertEqual(False, result["stalled"])

    def test_invalid_probe_output_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "python.exe").write_text("#", encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts" / "mt5_xau_tick_health_once.py").write_text("#", encoding="utf-8")
            config = make_config(root)

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output") + 1])
                output.write_text("garbage\n", encoding="utf-8")

            with patch("local_console.ticks.subprocess.run", side_effect=fake_run):
                result = capture_tick_health(config, "job1")

        self.assertEqual(False, result["available"])
        self.assertIn("无效", result["reason"])

    def test_probe_crash_is_unavailable_not_an_exception(self):
        import subprocess as real_subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "python.exe").write_text("#", encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts" / "mt5_xau_tick_health_once.py").write_text("#", encoding="utf-8")
            config = make_config(root)

            with patch(
                "local_console.ticks.subprocess.run",
                side_effect=real_subprocess.TimeoutExpired(cmd="x", timeout=30),
            ):
                result = capture_tick_health(config, "job1")

        self.assertEqual(False, result["available"])
        self.assertIn("采集失败", result["reason"])


if __name__ == "__main__":
    unittest.main()
