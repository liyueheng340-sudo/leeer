from __future__ import annotations

import unittest

from local_console.resonance import _timeframe_vote, compute_resonance


def frame(body: str, change_4: float) -> dict[str, object]:
    return {"body_direction": body, "change_4": change_4}


class TimeframeVoteTests(unittest.TestCase):
    def test_body_and_momentum_agree_votes_direction(self):
        self.assertEqual(1, _timeframe_vote(frame("buy", 5.0)))
        self.assertEqual(-1, _timeframe_vote(frame("sell", -5.0)))

    def test_body_and_momentum_conflict_votes_zero(self):
        self.assertEqual(0, _timeframe_vote(frame("buy", -5.0)))
        self.assertEqual(0, _timeframe_vote(frame("sell", 5.0)))

    def test_missing_momentum_falls_back_to_body(self):
        self.assertEqual(1, _timeframe_vote({"body_direction": "buy"}))

    def test_non_dict_frame_votes_zero(self):
        self.assertEqual(0, _timeframe_vote(None))


class ComputeResonanceTests(unittest.TestCase):
    def test_all_timeframes_bullish_gives_full_positive_score(self):
        snapshot = {
            "timeframe_structure": {
                "m5": frame("buy", 1.0),
                "m15": frame("buy", 2.0),
                "h1": frame("buy", 10.0),
                "h4": frame("buy", 30.0),
            }
        }
        result = compute_resonance(snapshot)
        self.assertTrue(result["available"])
        self.assertEqual(1.0, result["score"])
        self.assertEqual("共振偏多", result["label"])
        self.assertEqual(1.0, result["agreement"])
        self.assertEqual(4, result["voting_timeframes"])

    def test_high_timeframe_dominates_lower_ones_via_weight(self):
        # 仅 H4 看空，其余缺失：权重归一化后仍为满分看空
        snapshot = {"timeframe_structure": {"h4": frame("sell", -30.0)}}
        result = compute_resonance(snapshot)
        self.assertEqual(-1.0, result["score"])
        self.assertEqual("共振偏空", result["label"])

    def test_mixed_directions_labelled_conflict(self):
        snapshot = {
            "timeframe_structure": {
                "m5": frame("buy", 1.0),
                "m15": frame("buy", 2.0),
                "h1": frame("buy", 10.0),
                "h4": frame("sell", -30.0),  # 高时间框架反向，权重最大
            }
        }
        result = compute_resonance(snapshot)
        # 加权净方向 = (1+2+3-4)/10 = 0.2，落在冲突区间
        self.assertEqual(0.2, result["score"])
        self.assertEqual("方向冲突", result["label"])
        self.assertEqual(0.75, result["agreement"])

    def test_missing_structure_is_unavailable(self):
        result = compute_resonance({"bid": 4000.0})
        self.assertFalse(result["available"])
        self.assertIn("timeframe_structure", result["reason"])

    def test_conflicting_inner_signals_do_not_vote(self):
        # 每个时间框架内部 K 线方向与动量都矛盾 → 全部弃票 → 方向不明
        snapshot = {
            "timeframe_structure": {
                "m5": frame("buy", -1.0),
                "h1": frame("sell", 5.0),
            }
        }
        result = compute_resonance(snapshot)
        self.assertTrue(result["available"])
        self.assertEqual(0.0, result["score"])
        self.assertEqual("方向不明", result["label"])
        self.assertEqual(0, result["voting_timeframes"])
