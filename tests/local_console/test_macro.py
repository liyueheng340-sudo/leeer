from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_console.config import ConsoleConfig
from local_console.macro import MACRO_SERIES, fetch_macro_background

NOW = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)


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


def fred_response(values: list[tuple[str, str]]) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "observations": [{"date": date, "value": value} for date, value in values]
    }
    return response


def ok_side_effect(*_args, **_kwargs):
    # 与真实 FRED 一致：sort_order=desc 时最新观测在前
    return fred_response([("2026-07-30", "1.85"), ("2026-07-29", "1.80")])


class MacroBackgroundTests(unittest.TestCase):
    def test_missing_api_key_is_unavailable_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch.dict(os.environ, {}, clear=True), patch("local_console.macro.requests.get") as getter:
                result = fetch_macro_background(config, NOW)

        self.assertEqual("unavailable", result["status"])
        self.assertIn("FRED_API_KEY", result["reason"])
        getter.assert_not_called()

    def test_successful_fetch_returns_daily_background_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch.dict(os.environ, {"FRED_API_KEY": "test-key"}), patch("local_console.macro.requests.get", side_effect=ok_side_effect) as getter:
                result = fetch_macro_background(config, NOW)

        self.assertEqual("ok", result["status"])
        self.assertEqual("daily", result["frequency"])
        self.assertEqual(set(MACRO_SERIES), set(result["series"]))
        self.assertEqual(1.85, result["series"]["DFII10"]["latest"])
        self.assertEqual(0.05, result["series"]["DFII10"]["change_recent"])
        self.assertIn("日频", result["note"])
        self.assertEqual(getter.call_count, len(MACRO_SERIES))

    def test_second_call_uses_cache_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch.dict(os.environ, {"FRED_API_KEY": "test-key"}), patch("local_console.macro.requests.get", side_effect=ok_side_effect) as getter:
                first = fetch_macro_background(config, NOW)
                second = fetch_macro_background(config, NOW)

        self.assertEqual(first, second)
        self.assertEqual(getter.call_count, len(MACRO_SERIES))

    def test_expired_cache_refetches(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            config.state_dir.mkdir(parents=True, exist_ok=True)
            config.macro_cache_path.write_text(
                '{"status": "ok", "fetched_at": "2000-01-01T00:00:00+00:00", "series": {}}',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"FRED_API_KEY": "test-key"}), patch("local_console.macro.requests.get", side_effect=ok_side_effect) as getter:
                result = fetch_macro_background(config, NOW)

        self.assertEqual("ok", result["status"])
        self.assertEqual(getter.call_count, len(MACRO_SERIES))

    def test_all_series_failing_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch.dict(os.environ, {"FRED_API_KEY": "test-key"}), patch(
                "local_console.macro.requests.get",
                side_effect=ConnectionError("network down"),
            ):
                result = fetch_macro_background(config, NOW)

        self.assertEqual("unavailable", result["status"])
        self.assertIn("FRED 请求失败", result["reason"])

    def test_partial_failure_keeps_available_series(self):
        calls = {"count": 0}

        def flaky(*_args, **kwargs):
            calls["count"] += 1
            if kwargs.get("params", {}).get("series_id") == "DGS10":
                raise ConnectionError("one series down")
            return ok_side_effect()

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch.dict(os.environ, {"FRED_API_KEY": "test-key"}), patch("local_console.macro.requests.get", side_effect=flaky):
                result = fetch_macro_background(config, NOW)

        self.assertEqual("ok", result["status"])
        self.assertNotIn("DGS10", result["series"])
        self.assertEqual(len(MACRO_SERIES) - 1, len(result["series"]))
        self.assertIn("partial_errors", result)


if __name__ == "__main__":
    unittest.main()
