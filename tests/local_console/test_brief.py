from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_console.brief import (
    DEEP_MODEL_MAX_RETRIES,
    DEEP_MODEL_TIMEOUT_SECONDS,
    MODEL_MAX_RETRIES,
    MODEL_RETRY_BACKOFF_SECONDS,
    MODEL_TIMEOUT_SECONDS,
    PROMPT_VERSION,
    _invoke_with_retry,
    _parse_model_json,
    build_prompt,
    request_brief,
    validate_report,
    worst_case_seconds,
)
from local_console.config import ConsoleConfig
from local_console.guard import GateResult


def watch_payload() -> dict[str, object]:
    return {
        "action": "WATCH",
        "source_ids": ["mt5_snapshot"],
        "summary": "M1 结构混合，等待确认收盘。",
        "invalidation": "下一次快照到来后当前观察失效。",
        "next_observation": "下一根 M1 收盘后刷新。",
        "evidence_fields": ["bid", "timeframe_structure.h1.atr_14"],
    }


def analyse_payload() -> dict[str, object]:
    return {
        "action": "ANALYSE",
        "source_ids": ["mt5_snapshot", "verified_event_context"],
        "summary": "H1 结构偏强，回调幅度有限。",
        "invalidation": "收盘跌破入场区间下沿后失效。",
        "next_observation": "下一根 M15 收盘确认。",
        "evidence_fields": ["bid", "timeframe_structure.h1.atr_14"],
        "direction": "LONG",
        "entry_zone": "3995-4005",
        "take_profit": "4015",
        "stop_loss": "3985",
        "risk_note": "事件未核验，仓位需保守。",
    }


def analyse_snapshot() -> dict[str, object]:
    return {
        "bid": 4000.0,
        "ask": 4000.1,
        "symbol": "XAUUSD",
        "timeframe_structure": {"h1": {"atr_14": 20.0}},
    }


ANALYSE_GATE = GateResult("ANALYSE", True, True, "ok")
WATCH_GATE_NO_PLAN = GateResult("WATCH", True, False, "事件未核验")
WATCH_GATE_PLAN = GateResult("WATCH", True, True, "事件上下文未核验，允许技术面方向建议")


class BriefRequestTests(unittest.TestCase):
    def test_request_uses_a_bounded_qwen_call(self):
        config = ConsoleConfig(
            repo_root=Path("."),
            state_dir=Path("runtime"),
            mt5_python=Path("python.exe"),
            mt5_snapshot_script=Path("snapshot.py"),
            backend_url="https://example.invalid/v1",
            quick_model="quick",
            deep_model="deep",
        )
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(
            content='{"action":"WATCH","source_ids":["mt5_snapshot"],"summary":"中文摘要","invalidation":"中文失效条件","next_observation":"中文观察条件","evidence_fields":["bid"]}'
        )
        client = MagicMock()
        client.get_llm.return_value = llm

        with patch("local_console.brief.create_llm_client", return_value=client) as factory:
            payload = request_brief(
                config,
                "brief",
                {"symbol": "XAUUSD"},
                WATCH_GATE_NO_PLAN,
            )

        factory.assert_called_once_with(
            "qwen", "quick", "https://example.invalid/v1", timeout=MODEL_TIMEOUT_SECONDS, max_retries=0
        )
        self.assertEqual("WATCH", payload["action"])

    def test_request_can_use_qwen_default_endpoint_without_backend_url(self):
        config = ConsoleConfig(
            repo_root=Path("."),
            state_dir=Path("runtime"),
            mt5_python=Path("python.exe"),
            mt5_snapshot_script=Path("snapshot.py"),
            backend_url=None,
            quick_model="quick",
            deep_model="deep",
        )
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(
            content='{"action":"WATCH","source_ids":["mt5_snapshot"],"summary":"中文摘要","invalidation":"中文失效条件","next_observation":"中文观察条件","evidence_fields":["bid"]}'
        )
        client = MagicMock()
        client.get_llm.return_value = llm

        with patch("local_console.brief.create_llm_client", return_value=client) as factory:
            payload = request_brief(
                config,
                "brief",
                {"symbol": "XAUUSD"},
                WATCH_GATE_NO_PLAN,
            )

        factory.assert_called_once_with(
            "qwen", "quick", None, timeout=MODEL_TIMEOUT_SECONDS, max_retries=0
        )
        self.assertEqual("WATCH", payload["action"])

    def test_deep_review_uses_longer_timeout_and_deep_model(self):
        config = ConsoleConfig(
            repo_root=Path("."),
            state_dir=Path("runtime"),
            mt5_python=Path("python.exe"),
            mt5_snapshot_script=Path("snapshot.py"),
            backend_url="https://example.invalid/v1",
            quick_model="quick",
            deep_model="deep",
        )
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(
            content='{"action":"WATCH","source_ids":["mt5_snapshot"],"summary":"中文摘要","invalidation":"中文失效条件","next_observation":"中文观察条件","evidence_fields":["bid"]}'
        )
        client = MagicMock()
        client.get_llm.return_value = llm

        with patch("local_console.brief.create_llm_client", return_value=client) as factory:
            request_brief(config, "deep_review", {"symbol": "XAUUSD"}, WATCH_GATE_NO_PLAN)

        factory.assert_called_once_with(
            "qwen", "deep", "https://example.invalid/v1",
            timeout=DEEP_MODEL_TIMEOUT_SECONDS, max_retries=0,
        )
        # 推理模型显著更慢，深度复盘超时必须宽于快评。
        self.assertGreater(DEEP_MODEL_TIMEOUT_SECONDS, MODEL_TIMEOUT_SECONDS)


class InvokeWithRetryTests(unittest.TestCase):
    def test_transient_failure_is_retried_then_succeeds(self):
        calls = {"count": 0}

        def flaky_invoke(_prompt: str):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("网络抖动")
            return MagicMock(content='{"action": "WATCH"}')

        with patch("local_console.brief.time.sleep") as sleep:
            payload = _invoke_with_retry(flaky_invoke, "prompt")

        self.assertEqual({"action": "WATCH"}, payload)
        self.assertEqual(2, calls["count"])
        sleep.assert_called_once()  # 第二次尝试前退避一次

    def test_exhausts_retries_and_raises(self):
        calls = {"count": 0}

        def always_fails(_prompt: str):
            calls["count"] += 1
            raise RuntimeError("持续失败")

        with patch("local_console.brief.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "多次重试仍失败"):
                _invoke_with_retry(always_fails, "prompt")

        self.assertEqual(MODEL_MAX_RETRIES + 1, calls["count"])

    def test_zero_retries_calls_invoke_only_once(self):
        calls = {"count": 0}

        def always_fails(_prompt: str):
            calls["count"] += 1
            raise RuntimeError("持续偏慢")

        with patch("local_console.brief.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "多次重试仍失败"):
                _invoke_with_retry(always_fails, "prompt", max_retries=0)

        self.assertEqual(1, calls["count"])  # 深度复盘不重试：仅尝试一次


class ParseModelJsonTests(unittest.TestCase):
    """模型输出形式瑕疵（围栏/夹带文字）应被宽松解析，内容契约由 validate_report 把关。"""

    def test_parses_bare_json(self):
        self.assertEqual({"action": "WATCH"}, _parse_model_json('{"action": "WATCH"}'))

    def test_parses_markdown_fenced_json(self):
        content = '```json\n{"action": "WATCH", "summary": "观察"}\n```'
        self.assertEqual({"action": "WATCH", "summary": "观察"}, _parse_model_json(content))

    def test_parses_json_with_surrounding_prose(self):
        content = '好的，以下是分析：{"action": "WATCH"} 希望对你有帮助'
        self.assertEqual({"action": "WATCH"}, _parse_model_json(content))

    def test_nested_json_survives_prose_extraction(self):
        content = '前缀 {"action": "WATCH", "report": {"direction": "LONG"}} 后缀'
        self.assertEqual(
            {"action": "WATCH", "report": {"direction": "LONG"}}, _parse_model_json(content)
        )

    def test_non_json_still_raises(self):
        with self.assertRaisesRegex(RuntimeError, "not JSON"):
            _parse_model_json("完全不是 JSON 的回复")

    def test_fenced_json_preferred_when_prose_also_present(self):
        content = '说明文字 ```json\n{"action": "WATCH"}\n``` 补充 {干扰}'
        self.assertEqual({"action": "WATCH"}, _parse_model_json(content))

    def test_retry_path_uses_tolerant_parser(self):
        def fenced_invoke(_prompt: str):
            return MagicMock(content='```json\n{"action": "WATCH"}\n```')

        payload = _invoke_with_retry(fenced_invoke, "prompt", max_retries=0)

        self.assertEqual({"action": "WATCH"}, payload)  # 不再因围栏误判为瞬时故障


class WorstCaseBudgetTests(unittest.TestCase):
    def test_quick_path_includes_one_retry(self):
        self.assertEqual(
            MODEL_TIMEOUT_SECONDS * (MODEL_MAX_RETRIES + 1)
            + MODEL_RETRY_BACKOFF_SECONDS * MODEL_MAX_RETRIES,
            worst_case_seconds("brief"),
        )

    def test_deep_path_has_no_retry(self):
        self.assertEqual(0, DEEP_MODEL_MAX_RETRIES)
        self.assertEqual(DEEP_MODEL_TIMEOUT_SECONDS, worst_case_seconds("deep_review"))

    def test_stale_threshold_covers_both_paths(self):
        from local_console.service import STALE_THRESHOLD_SECONDS

        self.assertGreater(STALE_THRESHOLD_SECONDS, worst_case_seconds("brief"))
        self.assertGreater(STALE_THRESHOLD_SECONDS, worst_case_seconds("deep_review"))


class BriefValidationTests(unittest.TestCase):
    def test_report_with_unprovided_source_is_rejected(self):
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "Yahoo Finance"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("报告引用了未提供的数据源：Yahoo Finance", reason)
        self.assertIsNone(report)

    def test_watch_report_cannot_contain_direct_entry_instruction(self):
        payload = watch_payload()
        payload["summary"] = "立即买入。"

        accepted, reason, report = validate_report(payload, WATCH_GATE_NO_PLAN)

        self.assertFalse(accepted)
        self.assertEqual("观察模式报告包含直接入场指令", reason)
        self.assertIsNone(report)

    def test_valid_watch_report_is_available_to_the_ui(self):
        payload = watch_payload()

        accepted, reason, report = validate_report(payload, WATCH_GATE_NO_PLAN)

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertEqual(payload, report)

    def test_english_visible_report_is_rejected(self):
        payload = watch_payload()
        payload["summary"] = "Wait for a confirmed close."

        accepted, reason, report = validate_report(payload, WATCH_GATE_NO_PLAN)

        self.assertFalse(accepted)
        self.assertEqual("报告正文必须使用中文：summary", reason)
        self.assertIsNone(report)

    def test_english_dominated_report_with_token_chinese_is_rejected(self):
        payload = watch_payload()
        payload["summary"] = "中文 Wait for a confirmed close before entering the market."

        accepted, reason, report = validate_report(payload, WATCH_GATE_NO_PLAN)

        self.assertFalse(accepted)
        self.assertEqual("报告正文必须使用中文：summary", reason)
        self.assertIsNone(report)

    def test_watch_gate_rejects_analyse_action(self):
        payload = watch_payload()
        payload["action"] = "ANALYSE"

        accepted, reason, report = validate_report(payload, WATCH_GATE_NO_PLAN)

        self.assertFalse(accepted)
        self.assertEqual("观察模式报告动作必须是 WATCH", reason)
        self.assertIsNone(report)

    def test_valid_analyse_report_with_trade_plan_is_accepted(self):
        accepted, reason, report = validate_report(
            analyse_payload(), ANALYSE_GATE, analyse_snapshot()
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertIsNotNone(report)

    def test_directional_gate_requires_trade_keys(self):
        payload = analyse_payload()
        del payload["take_profit"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertIn("分析模式报告缺少交易建议字段", reason)
        self.assertIn("take_profit", reason)
        self.assertIsNone(report)

    def test_invalid_direction_is_rejected(self):
        payload = analyse_payload()
        payload["direction"] = "MAYBE"

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertIn("交易方向无效", reason)
        self.assertIsNone(report)

    def test_long_take_profit_below_entry_is_rejected(self):
        payload = analyse_payload()
        payload["take_profit"] = "3990"  # 多头止盈低于入场中点 4000

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("多头建议的止盈必须高于入场区间中点", reason)
        self.assertIsNone(report)

    def test_long_stop_loss_above_entry_is_rejected(self):
        payload = analyse_payload()
        payload["stop_loss"] = "4008"

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("多头建议的止损必须低于入场区间中点", reason)
        self.assertIsNone(report)

    def test_short_geometry_is_enforced_symmetrically(self):
        payload = analyse_payload()
        payload["direction"] = "SHORT"
        payload["take_profit"] = "3985"
        payload["stop_loss"] = "4015"

        accepted, _, _ = validate_report(payload, ANALYSE_GATE, analyse_snapshot())
        self.assertTrue(accepted)

        payload["stop_loss"] = "3990"  # 空头止损低于入场中点
        accepted, reason, _ = validate_report(payload, ANALYSE_GATE, analyse_snapshot())
        self.assertFalse(accepted)
        self.assertEqual("空头建议的止损必须高于入场区间中点", reason)

    def test_unparseable_price_field_is_rejected(self):
        payload = analyse_payload()
        payload["entry_zone"] = "等待确认"

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("交易建议字段无法解析为价格：entry_zone", reason)
        self.assertIsNone(report)

    def test_entry_far_from_snapshot_bid_is_rejected(self):
        payload = analyse_payload()
        payload["entry_zone"] = "4100-4110"  # 距 bid 超过 3×ATR(20)=60
        payload["take_profit"] = "4120"
        payload["stop_loss"] = "4090"

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("入场区间偏离快照报价超过允许波动", reason)
        self.assertIsNone(report)

    def test_take_profit_beyond_atr_limit_is_rejected(self):
        payload = analyse_payload()
        payload["take_profit"] = "4120"  # 距入场 120 > 5×ATR(20)=100

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("止盈距离入场超过 5 倍参考 ATR", reason)
        self.assertIsNone(report)

    def test_neutral_direction_skips_price_geometry(self):
        payload = analyse_payload()
        payload["direction"] = "NEUTRAL"
        payload["entry_zone"] = "不适用"
        payload["take_profit"] = "不适用"
        payload["stop_loss"] = "不适用"

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)

    def test_evidence_field_must_exist_in_facts(self):
        payload = analyse_payload()
        payload["evidence_fields"] = ["bid", "timeframe_structure.h1.rsi_14"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("依据字段不在已提供事实中：timeframe_structure.h1.rsi_14", reason)
        self.assertIsNone(report)

    def test_evidence_field_format_is_validated(self):
        payload = analyse_payload()
        payload["evidence_fields"] = ["看看 H1 的走势"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("依据字段列表无效：evidence_fields", reason)
        self.assertIsNone(report)

    def test_evidence_fields_are_required(self):
        payload = analyse_payload()
        del payload["evidence_fields"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertIn("evidence_fields", reason)
        self.assertIsNone(report)

    def test_macro_source_allowed_only_when_background_loaded(self):
        snapshot = analyse_snapshot()
        snapshot["background_macro"] = {"status": "ok", "series": {}}
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "verified_event_context", "fred_macro_background"]

        accepted, _, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted)

        snapshot["background_macro"] = {"status": "unavailable"}
        accepted, reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertFalse(accepted)
        self.assertEqual("报告引用了未提供的数据源：fred_macro_background", reason)

    def test_tick_source_allowed_only_when_sensor_available(self):
        snapshot = analyse_snapshot()
        snapshot["tick_health"] = {"available": True}
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "verified_event_context", "mt5_tick_health"]

        accepted, _, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted)

        snapshot["tick_health"] = {"available": False}
        accepted, reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertFalse(accepted)
        self.assertEqual("报告引用了未提供的数据源：mt5_tick_health", reason)

    def test_english_risk_note_is_rejected(self):
        payload = analyse_payload()
        payload["risk_note"] = "Event risk ahead, reduce size."

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("报告正文必须使用中文：risk_note", reason)
        self.assertIsNone(report)

    def test_watch_gate_with_plan_accepts_technical_direction(self):
        payload = analyse_payload()
        payload["action"] = "WATCH"
        payload["source_ids"] = ["mt5_snapshot"]

        accepted, reason, report = validate_report(payload, WATCH_GATE_PLAN, analyse_snapshot())

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertEqual("WATCH", report["action"])


class NewsSourceValidationTests(unittest.TestCase):
    """news_context 源的可用性与提示词克制规则。"""

    def test_news_source_allowed_only_when_loaded(self):
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {
            "status": "ok",
            "items": [{"title": "Gold surges", "publisher": "R", "utc": "", "summary": "", "link": ""}],
        }
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "verified_event_context", "news_context"]

        accepted, _, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted)

    def test_news_source_rejected_when_unavailable(self):
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {"status": "unavailable", "reason": "no net"}
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "verified_event_context", "news_context"]

        accepted, reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertFalse(accepted)
        self.assertEqual("报告引用了未提供的数据源：news_context", reason)

    def test_news_source_rejected_when_items_empty(self):
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {"status": "ok", "items": []}
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "verified_event_context", "news_context"]

        accepted, reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertFalse(accepted)
        self.assertEqual("报告引用了未提供的数据源：news_context", reason)

    def test_prompt_contains_news_restraint_rule(self):
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {
            "status": "ok",
            "items": [{"title": "Gold", "publisher": "R", "utc": "", "summary": "", "link": ""}],
        }
        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")
        self.assertIn("预期差", prompt)
        self.assertIn("反应式追单", prompt)
        self.assertIn("risk_note", prompt)

    def test_prompt_omits_news_rule_when_unavailable(self):
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {"status": "unavailable"}
        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")
        self.assertNotIn("反应式追单", prompt)
        self.assertNotIn("预期差", prompt)

    def test_prompt_version_upgraded_for_news(self):
        # 版本锁：1.2.0 加入事件时效交叉验证规则（past_events + 新闻优先）
        self.assertEqual("1.2.0", PROMPT_VERSION)

    def test_prompt_contains_event_cross_validation_rule(self):
        """verified_clear 状态的 prompt 应包含事件交叉验证规则。"""
        snapshot = analyse_snapshot()
        snapshot["event_context"] = {
            "status": "verified_clear",
            "current_utc": "2026-07-31T12:00:00+00:00",
            "next_event": {"title": "美国非农就业", "utc": "2026-08-01T12:30:00+00:00"},
            "past_events": [{"title": "美国核心 PCE", "utc": "2026-07-30T12:30:00+00:00"}],
        }
        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")
        self.assertIn("past_events", prompt)
        self.assertIn("已公布", prompt)
        self.assertIn("以新闻为准", prompt)
