from __future__ import annotations

import unittest

from local_console.regime import compute_market_regime


def frame(
    adx: float | None = None,
    rsi: float | None = None,
    stddev: float | None = None,
    body_direction: str | None = None,
    change_4: float | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {}
    if adx is not None:
        result["adx_14"] = adx
    if rsi is not None:
        result["rsi_14"] = rsi
    if stddev is not None:
        result["stddev_20"] = stddev
    if body_direction is not None:
        result["body_direction"] = body_direction
    if change_4 is not None:
        result["change_4"] = change_4
    return result


class RegimeTests(unittest.TestCase):
    """market_regime：双周期 ADX 判状态、StdDev 确认波动、RSI 极端标记（EA 精华参数）。"""

    def test_missing_structure_is_unavailable(self):
        result = compute_market_regime({"bid": 4000.0})

        self.assertFalse(result["available"])
        self.assertIn("timeframe_structure", result["reason"])

    def test_missing_indicators_is_unavailable(self):
        snapshot = {"timeframe_structure": {"m15": {"body_direction": "buy"}}}

        result = compute_market_regime(snapshot)

        self.assertFalse(result["available"])
        self.assertIn("指标", result["reason"])

    def test_dual_adx_above_threshold_is_trending(self):
        snapshot = {
            "timeframe_structure": {
                "m15": frame(adx=30.0, body_direction="buy", change_4=1.0),
                "h1": frame(adx=28.0, body_direction="buy", change_4=1.0),
            }
        }

        result = compute_market_regime(snapshot)

        self.assertTrue(result["available"])
        self.assertEqual("trending", result["regime"])
        self.assertEqual("buy", result["trend_direction"])

    def test_dual_adx_below_range_threshold_is_ranging(self):
        snapshot = {
            "timeframe_structure": {
                "m15": frame(adx=15.0),
                "h1": frame(adx=12.0),
            }
        }

        result = compute_market_regime(snapshot)

        self.assertEqual("ranging", result["regime"])
        self.assertIsNone(result["trend_direction"])

    def test_mixed_adx_is_transition(self):
        snapshot = {
            "timeframe_structure": {
                "m15": frame(adx=30.0),
                "h1": frame(adx=12.0),
            }
        }

        result = compute_market_regime(snapshot)

        self.assertEqual("transition", result["regime"])

    def test_single_frame_uses_its_own_adx(self):
        snapshot = {"timeframe_structure": {"m15": frame(adx=30.0)}}

        result = compute_market_regime(snapshot)

        self.assertEqual("trending", result["regime"])

    def test_trend_direction_uses_higher_timeframe_clear_vote(self):
        # m15 多空信号冲突（票 0），h1 明确偏空 → 趋势方向取 h1
        snapshot = {
            "timeframe_structure": {
                "m15": frame(adx=30.0, body_direction="buy", change_4=-1.0),
                "h1": frame(adx=28.0, body_direction="sell", change_4=-1.0),
            }
        }

        result = compute_market_regime(snapshot)

        self.assertEqual("trending", result["regime"])
        self.assertEqual("sell", result["trend_direction"])

    def test_volatility_confirmed_when_stddev_above_threshold(self):
        snapshot = {
            "timeframe_structure": {
                "m15": frame(adx=10.0, stddev=1.5),
                "h1": frame(adx=12.0),
            }
        }

        result = compute_market_regime(snapshot)

        self.assertTrue(result["volatility_confirmed"])

    def test_volatility_not_confirmed_below_threshold(self):
        snapshot = {
            "timeframe_structure": {
                "m15": frame(adx=10.0, stddev=0.8),
                "h1": frame(adx=12.0, stddev=1.0),
            }
        }

        result = compute_market_regime(snapshot)

        self.assertFalse(result["volatility_confirmed"])

    def test_rsi_overbought_extreme_takes_highest(self):
        snapshot = {
            "timeframe_structure": {
                "m15": frame(adx=10.0, rsi=88.0),
                "h1": frame(adx=12.0, rsi=60.0),
            }
        }

        result = compute_market_regime(snapshot)

        self.assertIsNotNone(result["rsi_extreme"])
        self.assertEqual("overbought", result["rsi_extreme"]["side"])
        self.assertEqual("m15", result["rsi_extreme"]["timeframe"])
        self.assertEqual(88.0, result["rsi_extreme"]["value"])

    def test_rsi_oversold_extreme_takes_lowest(self):
        snapshot = {
            "timeframe_structure": {
                "m15": frame(adx=10.0, rsi=10.0),
                "h1": frame(adx=12.0, rsi=45.0),
            }
        }

        result = compute_market_regime(snapshot)

        self.assertEqual("oversold", result["rsi_extreme"]["side"])
        self.assertEqual(10.0, result["rsi_extreme"]["value"])

    def test_no_rsi_extreme_in_middle_zone(self):
        snapshot = {
            "timeframe_structure": {
                "m15": frame(adx=10.0, rsi=50.0),
                "h1": frame(adx=12.0, rsi=60.0),
            }
        }

        result = compute_market_regime(snapshot)

        self.assertIsNone(result["rsi_extreme"])


if __name__ == "__main__":
    unittest.main()
