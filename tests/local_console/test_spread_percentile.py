from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_console.jobs import JobStore
from local_console.spread_percentile import (
    SPREAD_PERCENTILE_HISTORY_LIMIT,
    SPREAD_PERCENTILE_MIN_SAMPLES,
    _historical_spreads,
    compute_spread_percentile,
)


def _write_job(store: JobStore, spread_median: float) -> str:
    record = store.create("brief", "scalp")
    store.transition(
        record.id,
        "COMPLETE",
        "完成",
        gate={
            "action": "ANALYSE",
            "tick_health": {"available": True, "spread_median": spread_median},
        },
    )
    return record.id


class HistoricalSpreadsTest(unittest.TestCase):
    def test_collects_from_completed_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            for value in (0.2, 0.3, 0.5):
                _write_job(store, value)
            values = _historical_spreads(store, limit=10)
            self.assertEqual(sorted(values), [0.2, 0.3, 0.5])

    def test_skips_unavailable_tick_and_missing_median(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            record = store.create("brief", "scalp")
            store.transition(
                record.id, "COMPLETE", "完成",
                gate={"action": "ANALYSE", "tick_health": {"available": False}},
            )
            record2 = store.create("brief", "scalp")
            store.transition(
                record2.id, "COMPLETE", "完成",
                gate={"action": "ANALYSE", "tick_health": {"available": True}},
            )
            self.assertEqual(_historical_spreads(store, limit=10), [])

    def test_tolerates_corrupt_job_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            _write_job(store, 0.4)
            # 直接写入一个损坏 JSON
            (Path(tmp) / "deadbeef.json").write_text("{broken", encoding="utf-8")
            values = _historical_spreads(store, limit=10)
            self.assertEqual(values, [0.4])


class ComputeSpreadPercentileTest(unittest.TestCase):
    def test_percentile_rank(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            for value in (0.2, 0.4, 0.6, 0.8, 1.0):
                _write_job(store, value)
            pct = compute_spread_percentile(0.6, store)
            self.assertIsNotNone(pct)
            self.assertAlmostEqual(pct, 0.6, places=3)  # 4 个 <= 0.6 / 5

    def test_low_percentile_means_good_spread(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            for value in (0.5, 0.6, 0.7, 0.8, 0.9):
                _write_job(store, value)
            pct = compute_spread_percentile(0.5, store)
            self.assertAlmostEqual(pct, 0.2, places=3)

    def test_insufficient_samples_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            for value in (0.3, 0.5):
                _write_job(store, value)
            self.assertIsNone(compute_spread_percentile(0.5, store))

    def test_invalid_current_median_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            self.assertIsNone(compute_spread_percentile(None, store))
            self.assertIsNone(compute_spread_percentile(-1.0, store))

    def test_empty_store_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            self.assertIsNone(compute_spread_percentile(0.5, store))


class LimitsTest(unittest.TestCase):
    def test_min_samples_is_positive(self):
        self.assertGreaterEqual(SPREAD_PERCENTILE_MIN_SAMPLES, 5)

    def test_history_limit_sane(self):
        self.assertGreaterEqual(SPREAD_PERCENTILE_HISTORY_LIMIT, SPREAD_PERCENTILE_MIN_SAMPLES)


if __name__ == "__main__":
    unittest.main()
