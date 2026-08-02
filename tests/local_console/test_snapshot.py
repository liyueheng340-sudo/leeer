from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_console.config import ConsoleConfig
from local_console.snapshot import SnapshotCaptureError, capture_combined


def make_config(root: Path) -> ConsoleConfig:
    return ConsoleConfig(
        repo_root=root,
        state_dir=root / "runtime",
        mt5_python=root / "python.exe",
        mt5_snapshot_script=root / "context_script.py",
        backend_url=None,
        quick_model="quick",
        deep_model="deep",
    )


def prepare_config(root: Path) -> ConsoleConfig:
    (root / "python.exe").write_text("#", encoding="utf-8")
    (root / "context_script.py").write_text("#", encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "mt5_xau_snapshot_with_ticks_once.py").write_text("#", encoding="utf-8")
    return make_config(root)


CONTEXT_ROW = {"record": "market_context", "symbol": "XAUUSD", "bid": 4000.0}
TICK_ROW = {"record": "tick_health", "available": True, "ticks": 300, "spread_max": 0.15}


class CombinedCaptureTests(unittest.TestCase):
    def test_missing_combined_script_raises_capture_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))

            with self.assertRaises(SnapshotCaptureError):
                capture_combined(config, "job1")

    def test_tagged_records_are_split_into_snapshot_and_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            config = prepare_config(Path(directory))

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output") + 1])
                output.write_text(
                    json.dumps(CONTEXT_ROW) + "\n" + json.dumps(TICK_ROW) + "\n",
                    encoding="utf-8",
                )

            with patch("local_console.snapshot.subprocess.run", side_effect=fake_run):
                snapshot, tick_health = capture_combined(config, "job1")

        self.assertEqual("XAUUSD", snapshot["symbol"])
        self.assertEqual(4000.0, snapshot["bid"])
        self.assertNotIn("record", snapshot)
        self.assertEqual(True, tick_health["available"])
        self.assertEqual(300, tick_health["ticks"])

    def test_missing_tick_record_raises_capture_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config = prepare_config(Path(directory))

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output") + 1])
                output.write_text(json.dumps(CONTEXT_ROW) + "\n", encoding="utf-8")

            with patch("local_console.snapshot.subprocess.run", side_effect=fake_run), self.assertRaises(SnapshotCaptureError):
                capture_combined(config, "job1")


if __name__ == "__main__":
    unittest.main()
