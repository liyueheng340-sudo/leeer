"""Tests for statistical validation helpers (VARRD-inspired).

2026-08-07：Wilson 置信区间 / 显著性检验 / Bonferroni 校正 / edge decay 追踪。
确保"小样本 100% 胜率"被诚实地标记为不可靠（置信区间极宽、不显著）。
"""

from __future__ import annotations

import unittest

from local_console.statistical import (
    bonferroni_alpha,
    edge_decay,
    is_significant,
    wilson_interval,
    win_rate_with_ci,
)


class WilsonIntervalTests(unittest.TestCase):
    def test_zero_n_returns_none(self):
        self.assertIsNone(wilson_interval(0, 0))

    def test_full_win_small_n_has_wide_interval(self):
        # 3 单 100%：置信区间极宽（下限远低于 1），暴露小样本不可靠
        low, high = wilson_interval(3, 3)
        self.assertLess(low, 0.5)  # 下限应显著低于 1.0
        self.assertGreaterEqual(high, 0.9)

    def test_large_n_interval_narrows(self):
        # 100 单 60%：区间应比 3 单窄
        low, high = wilson_interval(60, 100)
        self.assertGreater(low, 0.5)
        self.assertLess(high, 0.7)

    def test_interval_bounds_are_valid(self):
        low, high = wilson_interval(40, 100)
        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)
        self.assertLessEqual(low, high)


class SignificanceTests(unittest.TestCase):
    def test_small_n_not_significant(self):
        # 3 单 100% 不显著（区间太宽，无法证明优于随机）
        self.assertFalse(is_significant(3, 3, p0=0.5))

    def test_large_n_significant(self):
        # 100 单 60% 显著高于 50%
        self.assertTrue(is_significant(60, 100, p0=0.5))

    def test_at_coinflip_not_significant(self):
        # 50/100 = 50%，不显著（等于随机）
        self.assertFalse(is_significant(50, 100, p0=0.5))

    def test_zero_n_not_significant(self):
        self.assertFalse(is_significant(0, 0))


class BonferroniTests(unittest.TestCase):
    def test_single_comparison_keeps_alpha(self):
        self.assertEqual(0.05, bonferroni_alpha(1))

    def test_eight_comparisons_divides_alpha(self):
        self.assertAlmostEqual(0.05 / 8, bonferroni_alpha(8))

    def test_zero_comparisons_returns_alpha(self):
        self.assertEqual(0.05, bonferroni_alpha(0))


class WinRateCITests(unittest.TestCase):
    def test_zero_n(self):
        result = win_rate_with_ci(0, 0)
        self.assertIsNone(result["win_rate"])
        self.assertIsNone(result["significant"])

    def test_small_n_notes_unreliable(self):
        result = win_rate_with_ci(3, 3)
        self.assertIn("样本不足", result["note"])
        self.assertFalse(result["significant"])

    def test_large_n_notes_reliable(self):
        result = win_rate_with_ci(60, 100)
        self.assertIn("样本充足", result["note"])
        self.assertTrue(result["significant"])


class EdgeDecayTests(unittest.TestCase):
    def test_insufficient_samples(self):
        result = edge_decay(["2026-01-01", "2026-01-02"], ["TP_FIRST", "SL_FIRST"])
        self.assertFalse(result["decayed"])
        self.assertIn("样本不足", result["note"])

    def test_no_decay_when_win_rate_improves(self):
        # 老段差、新段好 → 不衰减
        ts = [f"2026-01-{i:02d}" for i in range(1, 21)]
        out = ["SL_FIRST"] * 10 + ["TP_FIRST"] * 10
        result = edge_decay(ts, out)
        self.assertFalse(result["decayed"])
        self.assertTrue(result["buckets"])

    def test_decay_detected_when_latest_worse(self):
        # 老段好、新段差（共 30 样本分 3 段，每段 10，最新段 10 单全亏）→ 衰减
        ts = [f"2026-01-{i:02d}" for i in range(1, 31)]
        out = ["TP_FIRST"] * 20 + ["SL_FIRST"] * 10
        result = edge_decay(ts, out)
        self.assertTrue(result["decayed"])
        self.assertEqual(3, len(result["buckets"]))


if __name__ == "__main__":
    unittest.main()
