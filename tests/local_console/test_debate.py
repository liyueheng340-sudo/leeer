from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from local_console.config import ConsoleConfig
from local_console.debate import _invoke_model, run_debate
from local_console.guard import GateResult

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def make_config(root: Path) -> ConsoleConfig:
    return ConsoleConfig(
        repo_root=root,
        state_dir=root / "runtime",
        mt5_python=root / "python.exe",
        mt5_snapshot_script=root / "snapshot.py",
        backend_url="https://example.invalid/v1",
        quick_model="quick",
        deep_model="deep",
    )


def valid_report(direction: str) -> dict[str, object]:
    return {
        "action": "ANALYSE",
        "source_ids": ["mt5_snapshot"],
        "summary": "测试报告，方向明确。",
        "invalidation": "跌破结构低点失效。",
        "next_observation": "下一根 M15 收盘确认。",
        "evidence_fields": ["bid", "symbol"],
        "direction": direction,
        "entry_zone": "3995-4005",
        "take_profit": "4015" if direction == "LONG" else "3985",
        "stop_loss": "3990" if direction == "LONG" else "4010",
        "risk_note": "测试风险提示。",
        "suggestions": ["建议一", "建议二"],
        "scenarios": ["若破位离场", "若冲高减仓"],
        "avoid": ["不追突破"],
    }


def snapshot() -> dict[str, object]:
    return {
        "bid": 4000.0,
        "ask": 4000.1,
        "symbol": "XAUUSD",
        "timeframe_structure": {"h1": {"atr_14": 5.0}},
        "event_context": {"status": "verified_clear"},
    }


GATE = GateResult("ANALYSE", True, True, "ok", warnings=())


class DebateRoundTests(unittest.TestCase):
    """三家模型返回不同 JSON → 独立验收 → 共识合成。"""

    def _run(self, contents: dict[str, str], root: Path):
        """contents: {model: content_or_None}——None 表示调用失败。"""
        config = make_config(root)

        def fake_invoke_model(config_obj, provider, model, prompt):
            content = contents.get(model)
            if content is None:
                raise RuntimeError("simulated failure")
            return content

        with patch("local_console.debate._invoke_model", side_effect=fake_invoke_model):
            return run_debate(config, snapshot(), GATE, "swing")

    def test_three_agree_direction_consensus(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                {
                    "qwen3.7-max": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                    "deepseek-v4-flash-0731": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                    "glm-5.2": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                },
                Path(directory),
            )
        consensus = result["consensus"]
        self.assertEqual(3, consensus["valid_count"])
        self.assertEqual("LONG", consensus["direction"])
        self.assertEqual(2, len(result["rounds"]))  # 无分歧，不触发第 3 轮
        self.assertEqual("LONG", consensus["report"]["direction"])

    def test_split_vote_2_1_does_not_need_round3(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                {
                    "qwen3.7-max": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                    "deepseek-v4-flash-0731": __import__("json").dumps(valid_report("SHORT"), ensure_ascii=False),
                    "glm-5.2": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                },
                Path(directory),
            )
        consensus = result["consensus"]
        self.assertEqual(3, consensus["valid_count"])
        # 2 LONG vs 1 SHORT = 2/3 多数，已构成共识 → 无需第 3 轮
        self.assertEqual("LONG", consensus["direction"])
        self.assertEqual(2, len(result["rounds"]))
        self.assertEqual({"LONG": 2, "SHORT": 1, "NEUTRAL": 0}, consensus["report"]["debate_votes"])

    def test_three_way_split_triggers_round3(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                {
                    "qwen3.7-max": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                    "deepseek-v4-flash-0731": __import__("json").dumps(valid_report("SHORT"), ensure_ascii=False),
                    "glm-5.2": __import__("json").dumps(valid_report("NEUTRAL"), ensure_ascii=False),
                },
                Path(directory),
            )
        consensus = result["consensus"]
        self.assertEqual(3, consensus["valid_count"])
        # 1:1:1 三方分歧 → 触发第 3 轮收敛
        self.assertEqual(3, len(result["rounds"]))

    def test_one_failure_still_works_with_two_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                {
                    "qwen3.7-max": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                    "deepseek-v4-flash-0731": None,  # 调用失败
                    "glm-5.2": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                },
                Path(directory),
            )
        consensus = result["consensus"]
        self.assertEqual(2, consensus["valid_count"])
        self.assertEqual("LONG", consensus["direction"])
        # 失败家的 statement 应记录 error（每轮都尝试，均失败）
        failed = [s for r in result["rounds"] for s in r["statements"] if s.get("error")]
        self.assertGreaterEqual(len(failed), 1)

    def test_all_fail_no_report(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                {
                    "qwen3.7-max": None,
                    "deepseek-v4-flash-0731": None,
                    "glm-5.2": None,
                },
                Path(directory),
            )
        self.assertIsNone(result["consensus"]["report"])
        self.assertEqual(0, result["consensus"]["valid_count"])

    def test_bad_json_is_invalid_report(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                {
                    "qwen3.7-max": "这不是 JSON",
                    "deepseek-v4-flash-0731": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                    "glm-5.2": __import__("json").dumps(valid_report("LONG"), ensure_ascii=False),
                },
                Path(directory),
            )
        self.assertEqual(2, result["consensus"]["valid_count"])
        self.assertEqual("LONG", result["consensus"]["direction"])


class InvokeModelFallbackTests(unittest.TestCase):
    """B1：_invoke_model 主端点失败时切到备用端点。"""

    def make_fallback_config(self, root: Path) -> ConsoleConfig:
        return ConsoleConfig(
            repo_root=root,
            state_dir=root / "runtime",
            mt5_python=root / "python.exe",
            mt5_snapshot_script=root / "snapshot.py",
            backend_url="https://primary.invalid/v1",
            quick_model="quick",
            deep_model="deep",
            fallback_backend_url="https://fallback.invalid/v1",
            fallback_api_key="fallback-key",
        )

    def test_fallback_used_when_primary_fails(self):
        from unittest.mock import MagicMock

        def primary_llm():
            raise RuntimeError("primary down")

        fallback_llm = MagicMock()
        fallback_llm.invoke.return_value = MagicMock(content='{"ok": true}')

        def fake_client(provider, model, base_url=None, api_key=None, **kwargs):
            if "fallback" in str(base_url):
                return MagicMock(get_llm=lambda: fallback_llm)
            return MagicMock(get_llm=primary_llm)

        with tempfile.TemporaryDirectory() as directory:
            config = self.make_fallback_config(Path(directory))
            with patch("local_console.debate.create_llm_client", side_effect=fake_client) as client:
                result = _invoke_model(config, "qwen", "qwen3.7-max", "prompt")

        self.assertEqual('{"ok": true}', result)
        # 主端点 + 备用端点各被调用一次
        self.assertEqual(2, client.call_count)

    def test_no_fallback_when_not_configured(self):
        from unittest.mock import MagicMock

        def primary_llm():
            raise RuntimeError("primary down")

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))  # 无 fallback 配置
            with patch("local_console.debate.create_llm_client",
                       return_value=MagicMock(get_llm=primary_llm)) as client, self.assertRaises(RuntimeError):
                _invoke_model(config, "qwen", "qwen3.7-max", "prompt")

        self.assertEqual(2, client.call_count)  # DEBATE_RETRIES=1 重试主端点一次，无 fallback


if __name__ == "__main__":
    unittest.main()
