from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from local_console.config import ConsoleConfig
from local_console.jobs import JobStore
from local_console.review import (
    compute_context_stats,
    compute_forward_validation,
    compute_review_stats,
    due_for_review,
    evaluate_plan,
    evaluate_plan_with_costs,
    parse_trade_plan,
    run_due_reviews,
)

CREATED = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
NOW = CREATED + timedelta(hours=26)  # 超过 24h 复盘窗口


def long_plan() -> dict[str, object]:
    return {
        "direction": "LONG",
        "entry_lo": 3995.0,
        "entry_hi": 4005.0,
        "entry_mid": 4000.0,
        "take_profit": 4015.0,
        "stop_loss": 3985.0,
    }


def bar(minutes: int, high: float, low: float, close: float) -> dict[str, float]:
    return {
        "time": int((CREATED + timedelta(minutes=minutes)).timestamp()),
        "open": close,
        "high": high,
        "low": low,
        "close": close,
    }


def long_report() -> dict[str, object]:
    return {
        "direction": "LONG",
        "entry_zone": "3995-4005",
        "take_profit": "4015",
        "stop_loss": "3985",
    }


def short_report() -> dict[str, object]:
    return {
        "direction": "SHORT",
        "entry_zone": "3995-4005",
        "take_profit": "3985",
        "stop_loss": "4015",
    }


def gate_payload(
    action: str, version: str = "1.0.0", resonance: dict | None = None, iv: dict | None = None
) -> dict:
    payload = {"action": action, "prompt_version": version}
    if resonance is not None:
        payload["resonance"] = resonance
    if iv is not None:
        payload["iv"] = iv
    return payload


class ParseTradePlanTests(unittest.TestCase):
    def test_long_plan_parses(self):
        plan = parse_trade_plan(long_report())

        self.assertEqual("LONG", plan["direction"])
        self.assertEqual(4000.0, plan["entry_mid"])
        self.assertEqual(4015.0, plan["take_profit"])

    def test_neutral_plan_is_skipped(self):
        report = long_report()
        report["direction"] = "NEUTRAL"

        self.assertIsNone(parse_trade_plan(report))

    def test_unparseable_levels_are_skipped(self):
        report = long_report()
        report["take_profit"] = "不适用"

        self.assertIsNone(parse_trade_plan(report))


class EvaluatePlanTests(unittest.TestCase):
    def test_tp_first_is_a_win_with_r_multiple(self):
        bars = [bar(0, 4001, 3999, 4000), bar(5, 4016, 4002, 4015)]
        review = evaluate_plan(long_plan(), bars, CREATED, NOW)

        self.assertEqual("TP_FIRST", review["outcome"])
        self.assertEqual(1.0, review["r_multiple"])
        self.assertTrue(review["entry_touched"])

    def test_sl_first_is_a_full_loss(self):
        bars = [bar(0, 4001, 3999, 4000), bar(5, 4002, 3984, 3986)]
        review = evaluate_plan(long_plan(), bars, CREATED, NOW)

        self.assertEqual("SL_FIRST", review["outcome"])
        self.assertEqual(-1.0, review["r_multiple"])

    def test_same_bar_tp_and_sl_counts_as_sl_conservatively(self):
        # 同一根 K 线同时覆盖 TP(4015) 与 SL(3985)：顺序不可知，保守记止损
        bars = [bar(0, 4016, 3984, 4000)]
        review = evaluate_plan(long_plan(), bars, CREATED, NOW)

        self.assertEqual("SL_FIRST", review["outcome"])

    def test_entry_never_touched_after_window_is_not_triggered(self):
        bars = [bar(0, 4020, 4010, 4015), bar(5, 4025, 4012, 4020)]
        review = evaluate_plan(long_plan(), bars, CREATED, NOW)

        self.assertEqual("NOT_TRIGGERED", review["outcome"])
        self.assertIsNone(review["r_multiple"])

    def test_inside_window_without_decision_is_pending(self):
        early_now = CREATED + timedelta(hours=2)
        bars = [bar(0, 4020, 4010, 4015)]
        review = evaluate_plan(long_plan(), bars, CREATED, early_now)

        self.assertEqual("PENDING", review["outcome"])

    def test_touched_but_undecided_after_window_is_expired_with_floating(self):
        # 入场区间被触及（low 4004 ≤ 4005），但 TP/SL 均未命中
        bars = [bar(0, 4008, 4004, 4006), bar(5, 4009, 4005, 4007)]
        review = evaluate_plan(long_plan(), bars, CREATED, NOW)

        self.assertEqual("EXPIRED_UNRESOLVED", review["outcome"])
        self.assertEqual(7.0, review["floating_points"])

    def test_short_plan_is_mirrored(self):
        plan = long_plan()
        plan["direction"] = "SHORT"
        plan["take_profit"] = 3985.0
        plan["stop_loss"] = 4015.0
        bars = [bar(0, 4001, 3999, 4000), bar(5, 4002, 3984, 3986)]
        review = evaluate_plan(plan, bars, CREATED, NOW)

        self.assertEqual("TP_FIRST", review["outcome"])
        self.assertEqual(1.0, review["r_multiple"])


class StatsAndDueTests(unittest.TestCase):
    def make_config(self, root: Path) -> ConsoleConfig:
        return ConsoleConfig(
            repo_root=root,
            state_dir=root / "runtime",
            mt5_python=root / "python.exe",
            mt5_snapshot_script=root / "snapshot.py",
            backend_url=None,
            quick_model="quick",
            deep_model="deep",
        )

    def completed_job(self, store: JobStore, review: dict | None, gate: dict | None = None, report: dict | None = None) -> object:
        job = store.create("brief")
        record = store.get(job.id)
        record.stage = "COMPLETE"
        record.created_at = CREATED.isoformat()  # 固定创建时间，与合成 K 线对齐
        record.report = report if report is not None else long_report()
        if gate is not None:
            record.gate = gate
        if review is not None:
            record.review = review
        store._write(record)
        return store.get(job.id)

    def test_stats_aggregate_decided_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            self.completed_job(store, {"outcome": "TP_FIRST", "r_multiple": 1.0})
            self.completed_job(store, {"outcome": "SL_FIRST", "r_multiple": -1.0})
            self.completed_job(store, {"outcome": "NOT_TRIGGERED", "r_multiple": None})
            self.completed_job(store, {"outcome": "PENDING", "r_multiple": None})

            stats = compute_review_stats(store.list_recent())

        self.assertEqual(4, stats["reviewed"])
        self.assertEqual(2, stats["decided"])
        self.assertEqual(0.5, stats["win_rate"])
        self.assertEqual(0.0, stats["avg_r"])
        self.assertIn("不构成", stats["disclaimer"])

        # 统计验证层：小样本（n=2）即使 50% 胜率，CI 也承认不可靠、不显著。
        self.assertIsNotNone(stats["ci_low"])
        self.assertIsNotNone(stats["ci_high"])
        self.assertLessEqual(stats["ci_low"], 0.5)
        self.assertGreaterEqual(stats["ci_high"], 0.5)
        self.assertFalse(stats["significant"])

    def test_stats_large_sample_can_be_significant(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            # 100 单 65% 胜率：Wilson 下界 > 0.5 → 显著；n>=100 → 样本充足
            for _ in range(65):
                self.completed_job(store, {"outcome": "TP_FIRST", "r_multiple": 1.0})
            for _ in range(35):
                self.completed_job(store, {"outcome": "SL_FIRST", "r_multiple": -1.0})

            stats = compute_review_stats(store.list_recent(limit=200))

        self.assertEqual(100, stats["decided"])
        self.assertEqual(0.65, stats["win_rate"])
        self.assertTrue(stats["significant"])
        self.assertGreater(stats["ci_low"], 0.5)
        self.assertEqual("样本充足", stats["note"])

    def test_due_for_review_skips_decided_and_keeps_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            decided = self.completed_job(store, {"outcome": "TP_FIRST", "r_multiple": 1.0})
            pending = self.completed_job(store, {"outcome": "PENDING"})
            fresh = self.completed_job(store, None)

            due_ids = {record.id for record in due_for_review(store.list_recent(), NOW)}

        self.assertNotIn(decided.id, due_ids)
        self.assertIn(pending.id, due_ids)
        self.assertIn(fresh.id, due_ids)

    def test_run_due_reviews_writes_results_with_one_bars_call(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            store = JobStore(config.jobs_dir)
            job = self.completed_job(store, None)
            calls = []

            def fake_bars(_config, start, end, tag="review"):
                calls.append((start, end))
                return [bar(0, 4001, 3999, 4000), bar(5, 4016, 4002, 4015)]

            written = run_due_reviews(config, store, now=NOW, bars_runner=fake_bars)
            record = store.get(job.id)

        self.assertEqual(1, written)
        self.assertEqual(1, len(calls))
        self.assertEqual("TP_FIRST", record.review["outcome"])
        self.assertEqual(1.0, record.review["r_multiple"])
        self.assertIn("reviewed_at", record.review)

    def test_run_due_reviews_without_bars_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            store = JobStore(config.jobs_dir)
            job = self.completed_job(store, None)

            written = run_due_reviews(config, store, now=NOW, bars_runner=lambda *a, **k: [])
            record = store.get(job.id)

        self.assertEqual(0, written)
        self.assertIsNone(record.review)


class ContextStatsTests(unittest.TestCase):
    """情境复盘：按单维度切分，验证交易员关心的“什么情境下有 edge”。"""

    def completed_job(self, store, review, gate=None, report=None):
        job = store.create("brief")
        record = store.get(job.id)
        record.stage = "COMPLETE"
        record.created_at = CREATED.isoformat()
        record.report = report if report is not None else long_report()
        if gate is not None:
            record.gate = gate
        if review is not None:
            record.review = review
        store._write(record)
        return store.get(job.id)

    def test_groups_by_gate_action(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            self.completed_job(store, {"outcome": "TP_FIRST", "r_multiple": 1.0}, gate_payload("ANALYSE"))
            self.completed_job(store, {"outcome": "SL_FIRST", "r_multiple": -1.0}, gate_payload("ANALYSE"))
            self.completed_job(store, {"outcome": "TP_FIRST", "r_multiple": 1.0}, gate_payload("WATCH"))

            contexts = compute_context_stats(store.list_recent())

        self.assertEqual(2, contexts["by_gate_action"]["ANALYSE"]["decided"])
        self.assertEqual(0.5, contexts["by_gate_action"]["ANALYSE"]["win_rate"])
        self.assertEqual(1, contexts["by_gate_action"]["WATCH"]["decided"])
        self.assertEqual(1.0, contexts["by_gate_action"]["WATCH"]["win_rate"])
        # 统计验证层：8 维度多重比较，Bonferroni 门槛 = 0.05/8
        self.assertEqual(8, contexts["bonferroni_n"])
        self.assertAlmostEqual(0.05 / 8, contexts["bonferroni_alpha"], places=5)
        # 分组胜率同样带 CI（不显著的小样本被诚实暴露）
        analyse = contexts["by_gate_action"]["ANALYSE"]
        self.assertIsNotNone(analyse["ci_low"])
        self.assertFalse(analyse["significant"])

    def test_resonance_dimension_skips_unavailable(self):
        bull = {"available": True, "score": 0.8, "label": "共振偏多"}
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            self.completed_job(store, {"outcome": "TP_FIRST", "r_multiple": 1.0}, gate_payload("ANALYSE", resonance=bull))
            # 共振不可用的任务不应出现在共振维度聚合中
            self.completed_job(store, {"outcome": "SL_FIRST", "r_multiple": -1.0}, gate_payload("ANALYSE", resonance={"available": False}))

            contexts = compute_context_stats(store.list_recent())

        self.assertIn("共振偏多", contexts["by_resonance"])
        self.assertEqual(1, contexts["by_resonance"]["共振偏多"]["decided"])
        self.assertEqual(1, len(contexts["by_resonance"]))

    def test_groups_by_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            self.completed_job(store, {"outcome": "TP_FIRST", "r_multiple": 1.0}, gate_payload("ANALYSE"), long_report())
            self.completed_job(store, {"outcome": "SL_FIRST", "r_multiple": -1.0}, gate_payload("ANALYSE"), short_report())

            contexts = compute_context_stats(store.list_recent())

        self.assertEqual(1.0, contexts["by_direction"]["LONG"]["win_rate"])
        self.assertEqual(0.0, contexts["by_direction"]["SHORT"]["win_rate"])

    def test_groups_by_prompt_version(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            self.completed_job(store, {"outcome": "TP_FIRST", "r_multiple": 1.0}, gate_payload("ANALYSE", version="1.0.0"))
            self.completed_job(store, {"outcome": "SL_FIRST", "r_multiple": -1.0}, gate_payload("ANALYSE", version="1.1.0"))

            contexts = compute_context_stats(store.list_recent())

        self.assertEqual(1.0, contexts["by_prompt_version"]["1.0.0"]["win_rate"])
        self.assertEqual(0.0, contexts["by_prompt_version"]["1.1.0"]["win_rate"])

    def test_groups_by_vol_regime(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            self.completed_job(
                store,
                {"outcome": "TP_FIRST", "r_multiple": 1.0},
                gate_payload("ANALYSE", iv={"iv_vs_hv": "high"}),
            )
            self.completed_job(
                store,
                {"outcome": "SL_FIRST", "r_multiple": -1.0},
                gate_payload("ANALYSE", iv={"iv_vs_hv": "high"}),
            )
            self.completed_job(
                store,
                {"outcome": "TP_FIRST", "r_multiple": 1.0},
                gate_payload("ANALYSE", iv={"iv_vs_hv": "low"}),
            )
            # IV 层不可用的任务不参与 vol_regime 维度聚合
            self.completed_job(
                store,
                {"outcome": "SL_FIRST", "r_multiple": -1.0},
                gate_payload("ANALYSE", iv=None),
            )

            contexts = compute_context_stats(store.list_recent())

        self.assertEqual(0.5, contexts["by_vol_regime"]["vol_high"]["win_rate"])
        self.assertEqual(1.0, contexts["by_vol_regime"]["vol_low"]["win_rate"])
        self.assertNotIn("vol_na", contexts["by_vol_regime"])

    def test_jobs_without_review_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            self.completed_job(store, None, gate_payload("ANALYSE"))  # 无复盘结论

            contexts = compute_context_stats(store.list_recent())

        self.assertEqual({}, contexts["by_gate_action"])
        self.assertIn("不构成", contexts["disclaimer"])


class ForwardValidationTests(unittest.TestCase):
    """2026-08-07：前向验证——最近窗口 vs 更早窗口的期望 R 对比。"""

    def make_config(self, root: Path) -> ConsoleConfig:
        return ConsoleConfig(
            repo_root=root,
            state_dir=root / "runtime",
            mt5_python=root / "python.exe",
            mt5_snapshot_script=root / "snapshot.py",
            backend_url=None,
            quick_model="quick",
            deep_model="deep",
        )

    def _job_with(self, store: JobStore, created: datetime, outcome: str, r: float | None) -> None:
        job = store.create("brief")
        record = store.get(job.id)
        record.stage = "COMPLETE"
        record.created_at = created.isoformat()
        record.report = long_plan()
        record.review = {"outcome": outcome, "r_multiple": r}
        store._write(record)

    def test_splits_recent_and_earlier_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
            # 更早 10 单：全亏
            for i in range(10):
                self._job_with(store, base + timedelta(hours=i), "SL_FIRST", -1.0)
            # 最近 10 单：全赢
            for i in range(10):
                self._job_with(store, base + timedelta(days=1, hours=i), "TP_FIRST", 1.0)

            result = compute_forward_validation(store.list_recent(), recent_n=10)

        self.assertEqual(10, result["recent_n"])
        self.assertEqual(10, result["earlier_n"])
        self.assertEqual(1.0, result["recent"]["avg_r"])
        self.assertEqual(-1.0, result["earlier"]["avg_r"])
        self.assertEqual(1.0, result["recent"]["win_rate"])
        self.assertEqual(0.0, result["earlier"]["win_rate"])
        # edge_decay：20 已判定样本按时间切 3 段；本场景新近段更强，无衰减
        self.assertIn("edge_decay", result)
        self.assertEqual(3, len(result["edge_decay"]["buckets"]))
        self.assertFalse(result["edge_decay"]["decayed"])

    def test_edge_decay_detects_win_rate_fade(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
            # 最早 10 单全赢 → 中间 10 单 5/5 → 最近 10 单全亏：胜率随时间衰减
            for i in range(10):
                self._job_with(store, base + timedelta(hours=i), "TP_FIRST", 1.0)
            for i in range(10):
                self._job_with(store, base + timedelta(days=1, hours=i), "TP_FIRST" if i % 2 == 0 else "SL_FIRST", 1.0 if i % 2 == 0 else -1.0)
            for i in range(10):
                self._job_with(store, base + timedelta(days=2, hours=i), "SL_FIRST", -1.0)

            result = compute_forward_validation(store.list_recent(limit=200))

        self.assertTrue(result["edge_decay"]["decayed"])
        buckets = result["edge_decay"]["buckets"]
        self.assertEqual(3, len(buckets))
        # 验证单调衰减：最早段胜率 > 最近段胜率
        self.assertGreater(buckets[0]["win_rate"], buckets[-1]["win_rate"])

    def test_no_decided_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            self._job_with(store, CREATED, "PENDING", None)

            result = compute_forward_validation(store.list_recent())

        self.assertEqual(0, result["recent_n"])
        self.assertEqual(0, result["earlier_n"])
        self.assertIn("暂无", result["note"])

    def test_non_tp_sl_outcomes_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            self._job_with(store, CREATED, "NOT_TRIGGERED", None)
            self._job_with(store, CREATED, "TP_FIRST", 2.0)

            result = compute_forward_validation(store.list_recent(), recent_n=5)

        self.assertEqual(1, result["recent_n"])
        self.assertEqual(2.0, result["recent"]["avg_r"])


class DirectionQualityTests(unittest.TestCase):
    """2026-08-07 P0：方向判定 + 方向×结果四分格。"""

    def test_long_direction_correct_when_moves_up_beyond_threshold(self):
        # LONG risk=15, 阈值=7.5。high 触及 4010(≥4007.5) 但随后被扫损 → 方向对但点位差
        bars = [bar(0, 4001, 3999, 4000), bar(5, 4010, 4002, 4005), bar(10, 4004, 3984, 3986)]
        review = evaluate_plan(long_plan(), bars, CREATED, NOW)

        self.assertEqual("SL_FIRST", review["outcome"])  # 被扫损
        self.assertTrue(review["direction_correct"])  # 但方向看对了

    def test_long_direction_wrong_when_never_advances(self):
        # 从未向 LONG 方向移动 ≥7.5 → 方向错
        bars = [bar(0, 4001, 3999, 4000), bar(5, 4002, 3984, 3986)]
        review = evaluate_plan(long_plan(), bars, CREATED, NOW)

        self.assertEqual("SL_FIRST", review["outcome"])
        self.assertFalse(review["direction_correct"])

    def test_short_direction_correct_when_moves_down(self):
        # 构造 SHORT plan：entry_mid=4000, TP=3985, SL=4015, risk=15, 阈值=7.5
        plan = {
            "direction": "SHORT", "entry_lo": 3995.0, "entry_hi": 4005.0,
            "entry_mid": 4000.0, "take_profit": 3985.0, "stop_loss": 4015.0,
        }
        # 先向下触及 3992(方向对,≥7.5 无非), 再反弹被 4015 SL 扫 → 方向对但点位差
        bars = [bar(0, 4001, 3999, 4000), bar(5, 3998, 3992, 3995), bar(10, 4016, 4000, 4014)]
        review = evaluate_plan(plan, bars, CREATED, NOW)

        self.assertEqual("SL_FIRST", review["outcome"])
        self.assertTrue(review["direction_correct"])

    def test_quadrant_stats(self):
        from local_console.review import compute_direction_quality

        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            # 方向对+TP
            self._job_with(store, CREATED, "TP_FIRST", 1.0, dc=True)
            # 方向对+SL（点位差）
            self._job_with(store, CREATED, "SL_FIRST", None, dc=True)
            # 方向错+TP（运气）
            self._job_with(store, CREATED, "TP_FIRST", 2.0, dc=False)
            # 方向错+SL（真失败）
            self._job_with(store, CREATED, "SL_FIRST", None, dc=False)

            result = compute_direction_quality(store.list_recent())

        self.assertEqual(1, result["dir_correct_tp"]["n"])
        self.assertEqual(1, result["dir_correct_sl"]["n"])
        self.assertEqual(1, result["dir_wrong_tp"]["n"])
        self.assertEqual(1, result["dir_wrong_sl"]["n"])
        self.assertEqual(0.5, result["direction_correct_rate"])

    # 复用 ForwardValidationTests 的 _job_with（需支持 dc 参数）
    def _job_with(self, store, created, outcome, r, dc=True):
        job = store.create("brief")
        record = store.get(job.id)
        record.stage = "COMPLETE"
        record.created_at = created.isoformat()
        record.report = long_plan()
        record.review = {"outcome": outcome, "r_multiple": r, "direction_correct": dc}
        store._write(record)


class EvaluatePlanWithCostsTests(unittest.TestCase):
    """回测可信性增强：evaluate_plan_with_costs 的成本扣除与 intra-bar 判定。"""

    def test_cost_reduces_win_r_multiple(self):
        # LONG risk=15, TP=4015, SL=3985。bar 触及 TP(4016)。
        # 无成本 r=1.0；spread=3 → cost_r=3/15=0.2 → r=0.8
        bars = [bar(0, 4001, 3999, 4000), bar(5, 4016, 4002, 4015)]
        result = evaluate_plan_with_costs(long_plan(), bars, CREATED, NOW, spread=3.0)
        self.assertEqual("TP_FIRST", result["outcome"])
        self.assertAlmostEqual(0.8, result["r_multiple"], places=3)
        self.assertAlmostEqual(0.2, result["cost_r"], places=4)

    def test_zero_spread_no_cost(self):
        bars = [bar(0, 4001, 3999, 4000), bar(5, 4016, 4002, 4015)]
        result = evaluate_plan_with_costs(long_plan(), bars, CREATED, NOW, spread=0.0)
        self.assertEqual("TP_FIRST", result["outcome"])
        self.assertEqual(1.0, result["r_multiple"])
        self.assertNotIn("cost_r", result)

    def test_intra_bar_simultaneous_tp_sl_uses_distance_ratio(self):
        # 同一根 K 线 high=4016(触TP) low=3984(触SL)。close=4000。
        # dist_tp=|4000-4015|=15, dist_sl=|4000-3985|=15, ratio=15/30=0.5 → 判 TP
        bars = [bar(0, 4001, 3999, 4000), bar(5, 4016, 3984, 4000)]
        result = evaluate_plan_with_costs(long_plan(), bars, CREATED, NOW)
        self.assertEqual("TP_FIRST", result["outcome"])
        self.assertAlmostEqual(0.5, result["intra_bar"], places=3)

    def test_intra_bar_close_to_sl_judges_sl(self):
        # close=3986 更靠近 SL(3985)：dist_sl=1 小 → ratio 小 → 判 SL
        bars = [bar(0, 4001, 3999, 4000), bar(5, 4016, 3984, 3986)]
        result = evaluate_plan_with_costs(long_plan(), bars, CREATED, NOW)
        self.assertEqual("SL_FIRST", result["outcome"])
        self.assertIn("intra_bar", result)

    def test_pending_ignores_cost(self):
        bars = [bar(0, 4001, 3999, 4000)]
        result = evaluate_plan_with_costs(long_plan(), bars, CREATED, CREATED + timedelta(hours=1), spread=3.0)
        self.assertEqual("PENDING", result["outcome"])
        self.assertIsNone(result["r_multiple"])


if __name__ == "__main__":
    unittest.main()
