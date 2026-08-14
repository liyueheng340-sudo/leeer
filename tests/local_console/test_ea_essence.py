"""Unit tests for EA-essence additions (2026-08-06, 168EA folder distillation).

覆盖：fractal_levels（Gold Trade Pro 分形突破位）、macd_divergence（MACD 背离）、
signal_votes（king-v2 多策略投票）、regime CCI/EMA 扩展、guard 新标注。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from local_console.fractal_levels import compute_fractal_levels
from local_console.guard import cci_extreme_reason, ema_extension_reason, evaluate_gate
from local_console.macd_divergence import compute_macd_divergence
from local_console.regime import compute_market_regime
from local_console.resonance import compute_signal_votes


def make_bars(closes: list[float], *, step_seconds: int = 86400) -> list[dict[str, float]]:
    bars = []
    for i, close in enumerate(closes):
        bars.append(
            {
                "time": 1700000000 + i * step_seconds,
                "open": close,
                "high": close + 0.8,
                "low": close - 0.8,
                "close": close,
            }
        )
    return bars


class FractalLevelsTests(unittest.TestCase):
    def test_computes_levels_from_d1_bars(self):
        closes = [4000.0 + 5 * ((-1) ** (i // 5)) * (i % 5) for i in range(40)]
        result = compute_fractal_levels({"d1_bars": make_bars(closes), "bid": 4000.0, "ask": 4000.1})
        self.assertTrue(result["available"])
        self.assertIsInstance(result["buy_levels"], list)
        self.assertIsInstance(result["sell_levels"], list)
        self.assertIn("nearest_buy", result)
        self.assertIn("nearest_sell", result)
        self.assertIsInstance(result["reference_atr"], (int, float))

    def test_missing_data_unavailable(self):
        self.assertFalse(compute_fractal_levels({"bid": 4000.0})["available"])
        self.assertFalse(compute_fractal_levels({"d1_bars": make_bars([4000.0] * 5)})["available"])


class MacdDivergenceTests(unittest.TestCase):
    def test_detects_bearish_divergence(self):
        closes = []
        base = 4000.0
        for i in range(30):
            closes.append(base + i * 0.5)
        peak1 = closes[-1]
        for i in range(15):
            closes.append(peak1 - i * 0.3)
        for _ in range(30):
            closes.append(closes[-1] + 0.2)
        peak2 = closes[-1]
        for _ in range(8):
            closes.append(closes[-1] - 0.4)
        self.assertGreater(peak2, peak1 + 0.5)
        series = {tf: make_bars(closes, step_seconds=300) for tf in ("m5", "m15", "h1")}
        result = compute_macd_divergence({"bar_series": series})
        self.assertTrue(result["available"])
        self.assertTrue(result["any_divergence"])
        for _tf, entry in result["divergences"].items():
            if entry:
                self.assertEqual(entry["side"], "bearish")

    def test_no_divergence_on_steady_uptrend(self):
        closes = [4000.0 + i * 0.5 for i in range(80)]
        series = {tf: make_bars(closes, step_seconds=300) for tf in ("m5", "m15", "h1")}
        result = compute_macd_divergence({"bar_series": series})
        self.assertTrue(result["available"])
        self.assertFalse(result["any_divergence"])

    def test_missing_series_unavailable(self):
        self.assertFalse(compute_macd_divergence({})["available"])


class SignalVotesTests(unittest.TestCase):
    def test_all_four_signals_vote(self):
        structure = {
            "h4": {"body_direction": "buy", "change_4": 2.0, "breakout_up": True, "range_location_8": 0.2, "macd_histogram": 1.5},
            "h1": {"body_direction": "buy", "change_4": 1.0, "breakout_up": True, "range_location_8": 0.1, "macd_histogram": 0.8},
            "m15": {"body_direction": "buy", "change_4": 0.5, "breakout_up": False, "range_location_8": 0.4, "macd_histogram": 0.2},
            "m5": {"body_direction": "buy", "change_4": 0.3, "breakout_up": False, "range_location_8": 0.6, "macd_histogram": -0.1},
        }
        result = compute_signal_votes({"timeframe_structure": structure})
        self.assertTrue(result["available"])
        self.assertEqual(set(result["signals"]), {"trend", "breakout", "pullback", "macd"})
        self.assertGreater(result["consensus"], 0)

    def test_missing_structure_unavailable(self):
        self.assertFalse(compute_signal_votes({})["available"])


class RegimeEaExtensionsTests(unittest.TestCase):
    def _snapshot(self, cci_m15=50.0, cci_h1=40.0, close_m15=4005.0, close_h1=4012.0):
        return {
            "timeframe_structure": {
                "m15": {
                    "body_direction": "buy", "change_4": 1.0, "adx_14": 30.0, "rsi_14": 60.0,
                    "stddev_20": 1.5, "cci_14": cci_m15, "ema_20": 4000.0, "atr_14": 10.0,
                    "close": close_m15,
                },
                "h1": {
                    "body_direction": "buy", "change_4": 1.0, "adx_14": 32.0, "rsi_14": 58.0,
                    "stddev_20": 1.4, "cci_14": cci_h1, "ema_20": 4010.0, "atr_14": 12.0,
                    "close": close_h1,
                },
            }
        }

    def test_cci_extreme_detected(self):
        regime = compute_market_regime(self._snapshot(cci_m15=120.0, cci_h1=110.0))
        self.assertEqual(regime["cci_extreme"]["side"], "overbought")
        warning = cci_extreme_reason(regime)
        self.assertIsNotNone(warning)
        self.assertIn("CCI", warning)

    def test_ema_extension_warns(self):
        # m15: close 4030 vs ema20 4000 = 30 / ATR10 = 3.0 ATR → 超阈值
        regime = compute_market_regime(self._snapshot(close_m15=4030.0))
        self.assertIsNotNone(regime["ema_extension"])
        warning = ema_extension_reason(regime)
        self.assertIsNotNone(warning)
        self.assertIn("延伸过度", warning)

    def test_normal_cci_ema_no_warning(self):
        regime = compute_market_regime(self._snapshot())
        self.assertIsNone(cci_extreme_reason(regime))
        self.assertIsNone(ema_extension_reason(regime))


class GuardNewWarningsTests(unittest.TestCase):
    def test_gate_carries_ea_essence_warnings(self):
        regime = compute_market_regime(
            {
                "timeframe_structure": {
                    "m15": {
                        "body_direction": "buy", "change_4": 1.0, "adx_14": 30.0, "rsi_14": 60.0,
                        "stddev_20": 1.5, "cci_14": 120.0, "ema_20": 4000.0, "atr_14": 10.0,
                        "close": 4030.0,
                    },
                    "h1": {
                        "body_direction": "buy", "change_4": 1.0, "adx_14": 32.0, "rsi_14": 58.0,
                        "stddev_20": 1.4, "cci_14": 110.0, "ema_20": 4010.0, "atr_14": 12.0,
                        "close": 4020.0,
                    },
                }
            }
        )
        gate = evaluate_gate(
            {
                "identity_match": True,
                "symbol": "XAUUSD",
                "bid": 4020.0,
                "ask": 4020.1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "timeframe_structure": regime["available"],
            },
            {"status": "verified_clear"},
            datetime.now(timezone.utc),
            mode="scalp",
            resonance={"available": True, "score": 0.7, "label": "共振偏多"},
            regime=regime,
        )
        self.assertEqual(gate.action, "ANALYSE")
        warnings_text = " ".join(gate.warnings)
        self.assertIn("延伸过度", warnings_text)
        self.assertIn("CCI", warnings_text)


if __name__ == "__main__":
    unittest.main()
