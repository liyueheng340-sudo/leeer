from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_console.config import ConsoleConfig
from local_console.macro import MACRO_SERIES, fetch_macro_background

NOW = datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)


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


def csv_ok_side_effect(url, *args, **kwargs):
    # 免 key CSV 端点：返回 observation_date,{SERIES} 两列，列名随请求的序列变化
    from unittest.mock import MagicMock

    series_id = kwargs.get("params", {}).get("id", "DFII10")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.text = (
        f"observation_date,{series_id}\n"
        f"2026-07-30,1.85\n2026-07-29,1.80\n"
    )
    return response


class MacroBackgroundTests(unittest.TestCase):
    def test_missing_api_key_falls_back_to_csv(self):
        # 无 FRED_API_KEY 时不再直接 unavailable，而是走免 key CSV 端点降级
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch.dict(os.environ, {}, clear=True), patch(
                "local_console.macro.requests.get", side_effect=csv_ok_side_effect
            ) as getter:
                result = fetch_macro_background(config, NOW)

        self.assertEqual("ok", result["status"])
        self.assertEqual(set(MACRO_SERIES), set(result["series"]))
        # 无 key 时全部走 CSV 端点
        for call in getter.call_args_list:
            self.assertIn("fredgraph.csv", call.args[0])

    def test_missing_api_key_csv_all_fail_is_unavailable(self):
        # 无 key 且 CSV 也全部失败 → unavailable
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch.dict(os.environ, {}, clear=True), patch(
                "local_console.macro.requests.get",
                side_effect=ConnectionError("network down"),
            ):
                result = fetch_macro_background(config, NOW)

        self.assertEqual("unavailable", result["status"])
        self.assertIn("FRED 请求失败", result["reason"])

    def test_api_failure_degrades_to_csv(self):
        # 官方 API 失败自动降级 CSV：官方端点抛错，CSV 端点成功
        calls = {"count": 0}

        def degrading(url, *args, **kwargs):
            calls["count"] += 1
            if "fredgraph.csv" in str(url):
                return csv_ok_side_effect(url, *args, **kwargs)
            raise ConnectionError("official api down")

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch.dict(os.environ, {"FRED_API_KEY": "test-key"}), patch(
                "local_console.macro.requests.get", side_effect=degrading
            ):
                result = fetch_macro_background(config, NOW)

        self.assertEqual("ok", result["status"])
        self.assertEqual(set(MACRO_SERIES), set(result["series"]))

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
