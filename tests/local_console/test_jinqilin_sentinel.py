"""Unit tests for 金麒麟 sentinel (single-direction trend risk for martingale-grid EAs)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from local_console.jinqilin_sentinel import compute_jinqilin_sentinel


class JinqilinSentinelTests(unittest.TestCase):
    def _resonance(self, score: float = 0.8) -> dict[str, object]:
        return {"available": True, "score": score, "label": "共振偏多" if score > 0 else "共振偏空"}

    def _regime(self, trending: bool = True, direction: str = "buy", **extra) -> dict[str, object]:
        regime: dict[str, object] = {
            "available": True,
            "regime": "trending" if trending else "ranging",
            "trend_direction": direction if trending else None,
            "volatility_confirmed": False,
        }
        regime.update(extra)
        return regime

    def test_aligned_trend_is_high_risk(self):
        """强趋势市 + 共振同向 → 至少 MEDIUM（单边行情高危）。"""
        sentinel = compute_jinqilin_sentinel(
            {"timeframe_structure": {}},
            resonance=self._resonance(0.8),
            regime=self._regime(trending=True, direction="buy"),
        )
        self.assertTrue(sentinel["available"])
        self.assertGreaterEqual(sentinel["risk_score"], 3)
        self.assertTrue(any("强趋势市且共振同向" in flag for flag in sentinel["flags"]))

    def test_critical_with_many_signals(self):
        """趋势同向 + 波动放大 + 区间边缘 + 点差高位 + 新闻 → CRITICAL。"""
        snapshot = {
            "timeframe_structure": {
                "h1": {"range_location_8": 0.05},
                "m15": {"range_location_8": 0.3},
            },
            "macd_divergence": {"available": True, "any_divergence": True},
        }
        event_context = {
            "status": "verified_clear",
            "next_event": {"title": "FOMC", "utc": (datetime.now(UTC) + timedelta(hours=6)).isoformat()},
        }
        tick_health = {"spread_percentile": 0.9}
        sentinel = compute_jinqilin_sentinel(
            snapshot,
            resonance=self._resonance(0.8),
            regime=self._regime(
                trending=True,
                direction="buy",
                volatility_confirmed=True,
                cci_extreme={"side": "overbought", "value": 120.0},
                ema_extension={"timeframe": "h1", "side": "above", "atr_distance": 3.0},
            ),
            tick_health=tick_health,
            event_context=event_context,
        )
        self.assertEqual(sentinel["risk_level"], "CRITICAL")
        self.assertGreaterEqual(sentinel["risk_score"], 8)
        self.assertIn("暂停金麒麟", sentinel["advice"])

    def test_calm_env_is_low(self):
        """震荡市 + 无其他信号 → LOW。"""
        sentinel = compute_jinqilin_sentinel(
            {"timeframe_structure": {"h1": {"range_location_8": 0.5}}},
            resonance={"available": True, "score": 0.1, "label": "方向不明"},
            regime=self._regime(trending=False, direction=None),
        )
        self.assertEqual(sentinel["risk_level"], "LOW")
        self.assertEqual(sentinel["flags"], [])

    def test_news_window_only_low(self):
        """仅新闻窗口 → LOW（1 分，单一信号不足以升级）。"""
        event_context = {
            "status": "verified_clear",
            "next_event": {"title": "NFP", "utc": (datetime.now(UTC) + timedelta(hours=12)).isoformat()},
        }
        sentinel = compute_jinqilin_sentinel(
            {"timeframe_structure": {"h1": {"range_location_8": 0.5}}},
            resonance={"available": True, "score": 0.1, "label": "方向不明"},
            regime=self._regime(trending=False, direction=None),
            event_context=event_context,
        )
        self.assertEqual(sentinel["risk_level"], "LOW")
        self.assertTrue(any("24 小时内高影响事件" in flag for flag in sentinel["flags"]))


if __name__ == "__main__":
    unittest.main()
