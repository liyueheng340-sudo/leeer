from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from local_console.config import ConsoleConfig
from local_console.iv import (
    _annualized_hv,
    _extract_slice_iv,
    _svi_fit_slice,
    fetch_iv_context,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


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


def sample_chain(spot: float = 371.5):
    """构造一个典型的 GLD 期权链：下行偏斜（put IV > call IV）。"""
    strikes = [360 + i for i in range(20)]
    calls, puts = [], []
    for k in strikes:
        # 下行偏斜：put IV 系统性高于 call IV；ATM 附近 IV 最低（微笑）
        kk = k - spot
        smile = 0.18 + 0.002 * abs(kk) + (0.0015 * kk if kk > 0 else -0.0025 * kk)
        calls.append({"strike": float(k), "impliedVolatility": smile})
        puts.append({"strike": float(k), "impliedVolatility": smile + 0.02})
    return calls, puts


class SviFitTests(unittest.TestCase):
    def test_fit_recovers_atm_iv_from_synthetic_smile(self):
        calls, puts = sample_chain()
        fit = _extract_slice_iv(calls, puts, spot=371.5)
        self.assertIsNotNone(fit)
        # 合成的 ATM IV ≈ 0.18（k=0 处微笑值）；拟合值应在合理范围
        self.assertGreater(fit["atm_iv"], 0.10)
        self.assertLess(fit["atm_iv"], 0.30)
        self.assertIsNotNone(fit["rho"])
        # 下行偏斜 → rho 应为负
        self.assertLess(fit["rho"], 0.0)

    def test_fit_returns_none_with_too_few_points(self):
        result = _svi_fit_slice(
            np.asarray([-0.1, 0.0, 0.1], dtype=float),
            np.asarray([0.2, 0.18, 0.21], dtype=float),
            np.ones(3, dtype=float),
        )
        self.assertIsNone(result)

    def test_extract_returns_none_without_puts(self):
        calls, _puts = sample_chain()
        self.assertIsNone(_extract_slice_iv(calls, [], spot=371.5))

    def test_extract_falls_back_to_straddle_on_noisy_data(self):
        # 近 ATM 数据正常但存在极端离群值 → 拟合 RMSE 超门槛 → 回退最近 strike straddle
        calls = [
            {"strike": 365.0, "impliedVolatility": 0.24},
            {"strike": 368.0, "impliedVolatility": 0.21},
            {"strike": 370.0, "impliedVolatility": 0.20},
            {"strike": 371.0, "impliedVolatility": 0.20},
            {"strike": 372.0, "impliedVolatility": 0.205},
            {"strike": 373.0, "impliedVolatility": 0.21},
            {"strike": 375.0, "impliedVolatility": 0.22},
            {"strike": 378.0, "impliedVolatility": 0.95},  # 离群：报价错误或深度异常
            {"strike": 382.0, "impliedVolatility": 0.95},
            {"strike": 386.0, "impliedVolatility": 0.95},
        ]
        puts = list(calls)
        fit = _extract_slice_iv(calls, puts, spot=371.5)
        self.assertIsNotNone(fit)
        # 回退到最近 strike（371/372）straddle 均值 ≈ 0.2025
        self.assertAlmostEqual(fit["atm_iv"], 0.2025, places=2)


class AnnualizedHvTests(unittest.TestCase):
    def test_hv_requires_sufficient_samples(self):
        self.assertIsNone(_annualized_hv([1.0] * 10, 20))

    def test_hv_of_constant_series_is_zero(self):
        self.assertAlmostEqual(_annualized_hv([1.0] * 30, 20), 0.0, places=6)


class FetchIvContextTests(unittest.TestCase):
    def test_unavailable_when_chain_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with patch("local_console.iv._fetch_gld_chain", return_value=None):
                result = fetch_iv_context(config, NOW)
            self.assertEqual("unavailable", result["status"])

    def test_ok_payload_shape_and_rank_accumulation(self):
        metrics = {
            "atm_iv": 0.23,
            "skew": 0.01,
            "rho": -0.3,
            "rmse": 0.02,
            "days_to_expiry": 33,
            "spot": 371.5,
            "expiry": "2026-09-04",
            "term_slope": 0.03,
            "short_iv": 0.20,
            "long_iv": 0.23,
        }
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with (
                patch("local_console.iv._fetch_gld_chain", return_value=metrics),
                patch("local_console.iv._annualized_hv", return_value=0.20),
            ):
                result = fetch_iv_context(config, NOW)
            self.assertEqual("ok", result["status"])
            self.assertAlmostEqual(0.23, result["atm_iv"])
            self.assertEqual("neutral", result["iv_vs_hv"])  # 0.23 vs 0.20，差 0.03 不超阈值
            self.assertIsNone(result["iv_rank"])  # 单样本不足
            self.assertEqual(1, result["rank_samples"])
            self.assertAlmostEqual(0.03, result["term_slope"])
            # 第二次调用应命中 6h 缓存（不重新抓取）
            with patch("local_console.iv._fetch_gld_chain", side_effect=AssertionError("should use cache")):
                result2 = fetch_iv_context(config, NOW)
            self.assertEqual("ok", result2["status"])

    def test_iv_vs_hv_high_and_low(self):
        metrics = {
            "atm_iv": 0.30,
            "skew": 0.01,
            "rho": -0.3,
            "rmse": 0.02,
            "days_to_expiry": 33,
            "spot": 371.5,
            "expiry": "2026-09-04",
            "term_slope": None,
            "short_iv": None,
            "long_iv": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with (
                patch("local_console.iv._fetch_gld_chain", return_value=metrics),
                patch("local_console.iv._annualized_hv", return_value=0.20),
            ):
                result = fetch_iv_context(config, NOW)
            self.assertEqual("high", result["iv_vs_hv"])  # 0.30 - 0.20 = 0.10 > 阈值
        metrics["atm_iv"] = 0.15
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            with (
                patch("local_console.iv._fetch_gld_chain", return_value=metrics),
                patch("local_console.iv._annualized_hv", return_value=0.20),
            ):
                result = fetch_iv_context(config, NOW)
            self.assertEqual("low", result["iv_vs_hv"])  # 0.20 - 0.15 = 0.05 > 阈值

    def test_rank_accumulates_across_calls_without_cache(self):
        metrics = {
            "atm_iv": 0.23,
            "skew": 0.01,
            "rho": -0.3,
            "rmse": 0.02,
            "days_to_expiry": 33,
            "spot": 371.5,
            "expiry": "2026-09-04",
            "term_slope": None,
            "short_iv": None,
            "long_iv": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            # 两次调用之间推进时间越过 6h 缓存，验证 rank 累积
            later = NOW + timedelta(hours=7)
            with (
                patch("local_console.iv._fetch_gld_chain", return_value=metrics),
                patch("local_console.iv._annualized_hv", return_value=0.20),
            ):
                fetch_iv_context(config, NOW)
                result = fetch_iv_context(config, later)
            self.assertEqual(2, result["rank_samples"])


if __name__ == "__main__":
    unittest.main()
