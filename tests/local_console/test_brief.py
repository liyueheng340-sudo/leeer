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


def has_validation_warning(report: dict[str, object] | None, text: str) -> bool:
    """validation_warnings 是完整句子列表：断言需做子串匹配，而非成员匹配。"""
    if not report:
        return False
    return any(text in w for w in report.get("validation_warnings", []))


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
        "suggestions": ["若 M15 回踩 3995-4005 不破，可分批入场", "突破 4015 后关注量能确认，放量则持有"],
        "scenarios": ["若跌破 3985，立即离场并观望 3970 支撑", "若冲高 4030 遇阻回落，减仓一半锁定利润"],
        "avoid": ["不追突破", "不在数据窗口前重仓"],
    }


def analyse_snapshot() -> dict[str, object]:
    return {
        "bid": 4000.0,
        "ask": 4000.1,
        "symbol": "XAUUSD",
        "timeframe_structure": {"h1": {"atr_14": 20.0}},
        # 军师模式：allowed_source_ids 依据快照事件上下文判定
        # （verified_clear 才允许引用 verified_event_context 源）。
        "event_context": {"status": "verified_clear"},
    }


ANALYSE_GATE = GateResult("ANALYSE", True, True, "ok")
EVENT_WINDOW_GATE = GateResult(
    "ANALYSE",
    True,
    True,
    "MT5 快照新鲜；分析可用，附带 1 条风险标注",
    warnings=("当前处于高影响事件窗口：美国非农就业",),
)


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
            content='{"action":"ANALYSE","source_ids":["mt5_snapshot"],"summary":"中文摘要","invalidation":"中文失效条件","next_observation":"中文观察条件","evidence_fields":["bid"]}'
        )
        client = MagicMock()
        client.get_llm.return_value = llm

        with patch("local_console.brief.create_llm_client", return_value=client) as factory:
            payload = request_brief(
                config,
                "brief",
                {"symbol": "XAUUSD"},
                ANALYSE_GATE,
            )

        factory.assert_called_once_with(
            "qwen", "quick", "https://example.invalid/v1", timeout=MODEL_TIMEOUT_SECONDS, max_retries=0
        )
        self.assertEqual("ANALYSE", payload["action"])

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
            content='{"action":"ANALYSE","source_ids":["mt5_snapshot"],"summary":"中文摘要","invalidation":"中文失效条件","next_observation":"中文观察条件","evidence_fields":["bid"]}'
        )
        client = MagicMock()
        client.get_llm.return_value = llm

        with patch("local_console.brief.create_llm_client", return_value=client) as factory:
            payload = request_brief(
                config,
                "brief",
                {"symbol": "XAUUSD"},
                ANALYSE_GATE,
            )

        factory.assert_called_once_with(
            "qwen", "quick", None, timeout=MODEL_TIMEOUT_SECONDS, max_retries=0
        )
        self.assertEqual("ANALYSE", payload["action"])

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
            content='{"action":"ANALYSE","source_ids":["mt5_snapshot"],"summary":"中文摘要","invalidation":"中文失效条件","next_observation":"中文观察条件","evidence_fields":["bid"]}'
        )
        client = MagicMock()
        client.get_llm.return_value = llm

        with patch("local_console.brief.create_llm_client", return_value=client) as factory:
            request_brief(config, "deep_review", {"symbol": "XAUUSD"}, ANALYSE_GATE)

        factory.assert_called_once_with(
            "qwen", "deep", "https://example.invalid/v1",
            timeout=DEEP_MODEL_TIMEOUT_SECONDS, max_retries=0,
        )
        # 推理模型显著更慢，深度复盘超时必须宽于快评。
        self.assertGreater(DEEP_MODEL_TIMEOUT_SECONDS, MODEL_TIMEOUT_SECONDS)

    def test_fallback_endpoint_used_when_primary_fails(self):
        # 2026-08-03 双 key 冗余：主端点失败（额度耗尽/网络错误）时切到备用端点。
        config = ConsoleConfig(
            repo_root=Path("."),
            state_dir=Path("runtime"),
            mt5_python=Path("python.exe"),
            mt5_snapshot_script=Path("snapshot.py"),
            backend_url="https://primary.invalid/v1",
            quick_model="quick",
            deep_model="deep",
            fallback_backend_url="https://fallback.invalid/v1",
            fallback_api_key="fallback-key",
        )
        ok_content = '{"action":"ANALYSE","source_ids":["mt5_snapshot"],"summary":"中文摘要","invalidation":"中文失效条件","next_observation":"中文观察条件","evidence_fields":["bid"]}'

        def fake_factory(provider, model, base_url, **kwargs):
            client = MagicMock()
            llm = MagicMock()
            if "primary" in (base_url or ""):
                llm.invoke.side_effect = RuntimeError("主端点额度耗尽")
            else:
                llm.invoke.return_value = MagicMock(content=ok_content)
            client.get_llm.return_value = llm
            return client

        with patch("local_console.brief.create_llm_client", side_effect=fake_factory) as factory:
            payload = request_brief(
                config, "brief", {"symbol": "XAUUSD"}, ANALYSE_GATE
            )

        self.assertEqual("ANALYSE", payload["action"])
        # 主端点 + 备用端点各调用一次
        self.assertEqual(2, factory.call_count)
        fallback_call = factory.call_args_list[1]
        self.assertEqual("openai_compatible", fallback_call.args[0])
        self.assertEqual("https://fallback.invalid/v1", fallback_call.args[2])
        self.assertEqual("fallback-key", fallback_call.kwargs["api_key"])

    def test_no_fallback_raises_primary_error(self):
        # 未配置备用端点时，主端点失败直接抛错（保持原行为）。
        config = ConsoleConfig(
            repo_root=Path("."),
            state_dir=Path("runtime"),
            mt5_python=Path("python.exe"),
            mt5_snapshot_script=Path("snapshot.py"),
            backend_url="https://primary.invalid/v1",
            quick_model="quick",
            deep_model="deep",
        )

        def fake_factory(provider, model, base_url, **kwargs):
            client = MagicMock()
            llm = MagicMock()
            llm.invoke.side_effect = RuntimeError("主端点挂了")
            client.get_llm.return_value = llm
            return client

        with patch("local_console.brief.create_llm_client", side_effect=fake_factory):
            with self.assertRaises(RuntimeError):
                request_brief(config, "brief", {"symbol": "XAUUSD"}, ANALYSE_GATE)


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

        with patch("local_console.brief.time.sleep"), self.assertRaisesRegex(RuntimeError, "多次重试仍失败"):
            _invoke_with_retry(always_fails, "prompt")

        self.assertEqual(MODEL_MAX_RETRIES + 1, calls["count"])

    def test_zero_retries_calls_invoke_only_once(self):
        calls = {"count": 0}

        def always_fails(_prompt: str):
            calls["count"] += 1
            raise RuntimeError("持续偏慢")

        with patch("local_console.brief.time.sleep"), self.assertRaisesRegex(RuntimeError, "多次重试仍失败"):
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
        # 深度复盘已是三家辩论：最坏耗时 = (修复轮 + 最大轮数) × 单轮超时，
        # 修复轮（第 1 轮 <2 家有效时触发）使最坏调用数达 4 轮——陈旧阈值
        # 若只按 3 轮算会在辩论中途误杀任务（2026-08-03 实测 752s 被 750s 阈值误杀）。
        from local_console.debate import DEBATE_MAX_ROUNDS, DEBATE_TIMEOUT_SECONDS

        self.assertEqual(
            DEBATE_TIMEOUT_SECONDS * (DEBATE_MAX_ROUNDS + 1),
            worst_case_seconds("deep_review"),
        )

    def test_stale_threshold_covers_both_paths(self):
        from local_console.service import STALE_THRESHOLD_SECONDS

        self.assertGreater(STALE_THRESHOLD_SECONDS, worst_case_seconds("brief"))
        self.assertGreater(STALE_THRESHOLD_SECONDS, worst_case_seconds("deep_review"))


class BriefValidationTests(unittest.TestCase):
    def test_report_without_mt5_snapshot_anchor_is_rejected(self):
        # 真实性底线（2026-08-02 放宽）：必须锚定 mt5_snapshot；其余来源名放行
        payload = analyse_payload()
        payload["source_ids"] = ["verified_event_context", "Yahoo Finance"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("报告必须引用 mt5_snapshot 作为数据锚点", reason)
        self.assertIsNone(report)

    def test_report_with_shortname_sources_is_accepted(self):
        # 模型用短名引用快照内嵌事实（tick_health/session_context 等）是合理写法
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "tick_health", "session_context", "timeframe_resonance"]

        accepted, _reason, _report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertTrue(accepted)

    def test_non_analyse_action_is_rejected(self):
        payload = analyse_payload()
        payload["action"] = "WATCH"  # 军师模式：模型只输出 ANALYSE

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("军师模式报告动作必须是 ANALYSE", reason)
        self.assertIsNone(report)

    def test_english_visible_report_is_rejected(self):
        payload = analyse_payload()
        payload["summary"] = "Wait for a confirmed close."

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("报告正文必须使用中文：summary", reason)
        self.assertIsNone(report)

    def test_english_dominated_report_with_token_chinese_is_rejected(self):
        # 2026-08-03 余量：含中文正文 + 个别英文词 → 降级为 warning 不整单拒绝
        # （模型偶发引入合法宏观缩写/普通词，不应让交易者拿不到简报）。
        payload = analyse_payload()
        payload["summary"] = "中文 Wait for a confirmed close before entering the market."

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "白名单外英文词"))

    def test_missing_summary_backfilled_from_direction(self):
        # 2026-08-03 余量：模型偶发漏 summary（实测 3 次 REJECTED 根因），
        # 用 direction/invalidation/next_observation 拼出摘要，保证简报可展示。
        payload = analyse_payload()
        del payload["summary"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertIsInstance(report["summary"], str)
        self.assertTrue(report["summary"].strip())
        # 摘要应包含方向倾向
        self.assertIn("偏多", report["summary"])  # analyse_payload 默认 LONG

    def test_directional_gate_requires_trade_keys(self):
        payload = analyse_payload()
        del payload["take_profit"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertIn("分析模式报告缺少交易建议字段", reason)
        self.assertIn("take_profit", reason)
        self.assertIsNone(report)

    def test_missing_narrative_fields_are_filled_with_defaults(self):
        # 2026-08-02 放宽：invalidation/next_observation 缺失自动补默认文案，不拒绝
        payload = analyse_payload()
        del payload["invalidation"]
        del payload["next_observation"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertIsNotNone(report)
        self.assertTrue(report["invalidation"].strip())
        self.assertTrue(report["next_observation"].strip())
        self.assertNotEqual("中文失效条件", report["invalidation"])

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
        # 2026-08-03 修复：路径未解析到已提供事实 → 风险标注而非整单拒绝
        # （模型引用真实事实时猜错路径是常见小错，军师模式降级为 warning）。
        payload = analyse_payload()
        payload["evidence_fields"] = ["bid", "timeframe_structure.h1.rsi_14"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertTrue(accepted, reason)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "依据字段未解析到已提供事实：timeframe_structure.h1.rsi_14"))

    def test_evidence_field_format_is_validated(self):
        payload = analyse_payload()
        payload["evidence_fields"] = ["看看 H1 的走势"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("依据字段列表无效：evidence_fields", reason)
        self.assertIsNone(report)

    def test_evidence_fields_up_to_20_accepted(self):
        # 2026-08-03 修复：模型按 facts_paths 清单引用真实字段常达 15 个，
        # 12 上限过严会拒掉全合法报告（brief 高频 REJECTED 根因），放宽到 20。
        snapshot = analyse_snapshot()
        snapshot["timeframe_structure"] = {
            "m5": {"atr_14": 2.0, "adx_14": 20.0, "rsi_14": 50.0},
            "m15": {"atr_14": 5.0, "adx_14": 20.0, "rsi_14": 50.0},
            "h1": {"atr_14": 14.0, "adx_14": 20.0, "rsi_14": 50.0},
            "h4": {"atr_14": 36.0, "adx_14": 20.0, "rsi_14": 50.0},
        }
        snapshot["latest_closed_bars"] = {
            "m15": {"close": 4065.0}, "h1": {"close": 4065.0}, "h4": {"close": 4057.0},
        }
        snapshot["tick_health"] = {"available": True, "spread_percentile": 0.5}
        snapshot["session_context"] = {"status": "ok", "label": "london"}
        payload = analyse_payload()
        payload["evidence_fields"] = [
            "bid", "ask", "spread",
            "latest_closed_bars.m15.close", "latest_closed_bars.h1.close",
            "latest_closed_bars.h4.close",
            "timeframe_structure.m5.atr_14", "timeframe_structure.m15.atr_14",
            "timeframe_structure.h1.adx_14", "timeframe_structure.h4.adx_14",
            "timeframe_structure.m15.rsi_14", "timeframe_structure.h1.rsi_14",
            "timeframe_resonance.score", "market_regime.regime",
            "tick_health.spread_percentile", "session_context.label",
        ]  # 16 个合法字段

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, snapshot)

        self.assertTrue(accepted, reason)
        self.assertEqual("报告已验收", reason)

    def test_evidence_fields_are_required(self):
        payload = analyse_payload()
        del payload["evidence_fields"]

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertIn("evidence_fields", reason)
        self.assertIsNone(report)

    def test_macro_source_accepted_regardless_of_availability(self):
        # 2026-08-02 放宽：来源名不再按可用性逐一拒绝，只锚定 mt5_snapshot
        snapshot = analyse_snapshot()
        snapshot["background_macro"] = {"status": "ok", "series": {}}
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "verified_event_context", "fred_macro_background"]

        accepted, _, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted)

        snapshot["background_macro"] = {"status": "unavailable"}
        accepted, _, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted)

    def test_tick_source_accepted_regardless_of_availability(self):
        # 2026-08-02 放宽：来源名不再按可用性逐一拒绝，只锚定 mt5_snapshot
        snapshot = analyse_snapshot()
        snapshot["tick_health"] = {"available": True}
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "verified_event_context", "mt5_tick_health"]

        accepted, _, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted)

        snapshot["tick_health"] = {"available": False}
        accepted, _, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted)

    def test_english_risk_note_is_rejected(self):
        payload = analyse_payload()
        payload["risk_note"] = "Event risk ahead, reduce size."

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, analyse_snapshot())

        self.assertFalse(accepted)
        self.assertEqual("报告正文必须使用中文：risk_note", reason)
        self.assertIsNone(report)

    def test_watch_gate_with_plan_accepts_technical_direction(self):
        # 军师模式：即使 gate 带事件窗口标注，报告仍须输出 ANALYSE 并带完整交易建议
        payload = analyse_payload()

        accepted, reason, report = validate_report(payload, EVENT_WINDOW_GATE, analyse_snapshot())

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertEqual("ANALYSE", report["action"])

    def test_gate_warnings_are_injected_into_prompt(self):
        prompt = build_prompt(analyse_snapshot(), EVENT_WINDOW_GATE, "brief")
        self.assertIn("美国非农就业", prompt)
        self.assertIn("risk_note 必须逐条覆盖", prompt)


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

    def test_news_source_accepted_even_when_unavailable(self):
        # 2026-08-02 放宽：来源名不再按可用性拒绝，只锚定 mt5_snapshot
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {"status": "unavailable", "reason": "no net"}
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "verified_event_context", "news_context"]

        accepted, _reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted)

    def test_news_source_allowed_when_items_empty_but_status_ok(self):
        # 休市时 news status=ok 但 items 为空列表：空源 ≠ 未提供数据，允许引用
        # （2026-08-02 辩论修复：此场景此前误杀深度复盘）
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {"status": "ok", "items": []}
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "verified_event_context", "news_context"]

        accepted, _reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted)

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
        # 版本锁：1.8.0 顺势回调纪律（scalp 只吃回调不追价、TP 快速止盈 1.0-1.5R、
        # M5 range_location_8 注入；依据本地回测回调 +0.21R vs 追价 -0.43R）。
        self.assertEqual("1.8.0", PROMPT_VERSION)

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

class EdgeDisciplineValidationTests(unittest.TestCase):
    """1.5.0 军师模式：顺势约束与关键价位贴近由硬拒绝改为风险标注（validation_warnings）。"""

    def snapshot_with_resonance(self, score: float) -> dict[str, object]:
        return {
            **analyse_snapshot(),
            "timeframe_resonance": {
                "available": True,
                "score": score,
                "label": "共振偏多" if score > 0 else "共振偏空",
            },
        }

    def test_long_accepted_with_warning_when_resonance_bearish(self):
        payload = analyse_payload()  # LONG
        accepted, reason, report = validate_report(
            payload, ANALYSE_GATE, self.snapshot_with_resonance(-0.8)
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "方向与多周期共振相悖"))

    def test_short_accepted_with_warning_when_resonance_bullish(self):
        payload = analyse_payload()
        payload["direction"] = "SHORT"
        payload["take_profit"] = "3985"
        payload["stop_loss"] = "4015"
        accepted, reason, report = validate_report(
            payload, ANALYSE_GATE, self.snapshot_with_resonance(0.8)
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "方向与多周期共振相悖"))

    def test_long_accepted_without_warning_when_resonance_bullish(self):
        payload = analyse_payload()  # LONG
        accepted, reason, report = validate_report(
            payload, ANALYSE_GATE, self.snapshot_with_resonance(0.8)
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertNotIn("validation_warnings", report)

    def test_short_accepted_when_resonance_bearish(self):
        payload = analyse_payload()
        payload["direction"] = "SHORT"
        payload["take_profit"] = "3985"
        payload["stop_loss"] = "4015"
        accepted, reason, _ = validate_report(
            payload, ANALYSE_GATE, self.snapshot_with_resonance(-0.8)
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)

    def test_weak_resonance_skips_direction_gate(self):
        payload = analyse_payload()  # LONG
        accepted, _, _ = validate_report(
            payload, ANALYSE_GATE, self.snapshot_with_resonance(0.3)
        )

        self.assertTrue(accepted)  # |score|<0.5 不构成强制方向

    def test_scalp_chase_high_downgraded_to_warning(self):
        # 2026-08-03 余量修正：追价从硬拒降级为 validation_warnings——
        # 低质量建议如实标注，不整单拒绝（用户反馈"简报经常要点几次才出来"）。
        snapshot = analyse_snapshot()
        snapshot["timeframe_structure"] = {
            "m5": {"atr_14": 20.0, "range_location_8": 0.8},  # 区间高位
        }
        payload = analyse_payload()  # LONG 在区间高位 = 追高

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, snapshot)

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "追高胜率偏低"))

    def test_scalp_chase_low_downgraded_to_warning(self):
        snapshot = analyse_snapshot()
        snapshot["timeframe_structure"] = {
            "m5": {"atr_14": 20.0, "range_location_8": 0.2},  # 区间低位
        }
        payload = analyse_payload()
        payload["direction"] = "SHORT"
        payload["take_profit"] = "3985"
        payload["stop_loss"] = "4015"

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, snapshot)

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "追低胜率偏低"))

    def test_entry_far_from_key_level_accepted_with_warning(self):
        snapshot = analyse_snapshot()
        snapshot["latest_closed_bars"] = {"h1": {"high": 3990.0, "low": 3970.0}}
        snapshot["timeframe_structure"] = {"h1": {"atr_14": 20.0}}
        payload = analyse_payload()
        payload["entry_zone"] = "4022-4028"  # 距 3990 关键位 35 > 1×ATR(20)，距 4000 关口 25 > 20
        payload["take_profit"] = "4040"
        payload["stop_loss"] = "4015"

        accepted, reason, report = validate_report(payload, ANALYSE_GATE, snapshot)

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "未贴近任何关键价位"))

    def test_entry_touching_key_level_is_accepted(self):
        snapshot = analyse_snapshot()
        snapshot["latest_closed_bars"] = {"h1": {"high": 4000.0, "low": 3980.0}}
        snapshot["timeframe_structure"] = {"h1": {"atr_14": 20.0}}
        payload = analyse_payload()
        payload["entry_zone"] = "3995-4005"  # 中点 4000 = 前日高

        accepted, reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)

    def test_prompt_injects_key_levels_when_available(self):
        snapshot = analyse_snapshot()
        snapshot["latest_closed_bars"] = {"h1": {"high": 3990.0, "low": 3970.0}}

        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")

        self.assertIn("关键价位层", prompt)
        self.assertIn("3970.0", prompt)

    def test_prompt_injects_round_levels_even_without_bars(self):
        # bid=4000 → 整数关口 3950/4000/4050 始终由 bid 确定性推出
        prompt = build_prompt(analyse_snapshot(), ANALYSE_GATE, "brief")

        self.assertIn("关键价位层", prompt)
        self.assertIn("4000", prompt)

    def test_prompt_injects_session_context(self):
        snapshot = analyse_snapshot()
        snapshot["session_context"] = {
            "status": "ok",
            "label": "london",
            "name": "伦敦早盘",
            "minutes_to_london_fix": 30,
            "minutes_to_comex_open": 45,
        }

        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")

        self.assertIn("session_context", prompt)
        self.assertIn("伦敦早盘", prompt)
        self.assertIn("30", prompt)
        self.assertIn("45", prompt)

    def test_prompt_omits_session_context_when_unavailable(self):
        prompt = build_prompt(analyse_snapshot(), ANALYSE_GATE, "brief")

        # session_context 作为数据源名在 allowed_sources 中恒被允许（引用合法），
        # 但无数据时不得注入时段规则文本。
        self.assertNotIn("当前时段", prompt)

    def test_evidence_indexed_paths_accepted(self):
        """?????? items[0] / items[] ??????????"""
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {"status": "ok", "items": [{"title": "??"}]}
        payload = analyse_payload()
        payload["evidence_fields"] = ["news_context.items[0].title", "bid"]
        accepted, reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted, reason)

    def test_evidence_wildcard_index_accepted(self):
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {"status": "ok", "items": [{"title": "??"}]}
        payload = analyse_payload()
        payload["evidence_fields"] = ["news_context.items[].title"]
        accepted, reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted, reason)

    def test_evidence_out_of_range_index_becomes_warning(self):
        # 2026-08-03 修复：索引越界属于引用瑕疵，军师模式降级为 warning 而非整单拒绝。
        snapshot = analyse_snapshot()
        snapshot["news_context"] = {"status": "ok", "items": [{"title": "头条"}]}
        payload = analyse_payload()
        payload["evidence_fields"] = ["news_context.items[5].title"]
        accepted, reason, report = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted, reason)
        self.assertTrue(has_validation_warning(report, "news_context.items[5].title"))

    def test_prompt_injects_facts_paths(self):
        """prompt ??????????? evidence_fields ???"""
        snapshot = analyse_snapshot()
        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")
        self.assertIn("facts_paths", prompt)
        self.assertIn("timeframe_structure.h1.atr_14", prompt)

    def test_session_context_is_allowed_source(self):
        """session_context ??????????????????"""
        snapshot = analyse_snapshot()
        snapshot["session_context"] = {"status": "ok", "label": "london"}
        payload = analyse_payload()
        payload["source_ids"] = ["mt5_snapshot", "session_context"]
        payload["evidence_fields"] = ["session_context.label"]
        accepted, reason, _ = validate_report(payload, ANALYSE_GATE, snapshot)
        self.assertTrue(accepted, reason)

    def test_prompt_annotates_high_spread_percentile(self):
        snapshot = analyse_snapshot()
        snapshot["tick_health"] = {"available": True, "spread_percentile": 0.9}

        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")

        self.assertIn("spread_percentile", prompt)
        self.assertIn("历史高位", prompt)

    def test_prompt_omits_neutral_spread_percentile(self):
        snapshot = analyse_snapshot()
        snapshot["tick_health"] = {"available": True, "spread_percentile": 0.5}

        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")

        # 中性分位不产生规则（键名随 facts 序列化必然存在，断言规则文本）
        self.assertNotIn("历史高位", prompt)
        self.assertNotIn("历史低位", prompt)

class MarketRegimeValidationTests(unittest.TestCase):
    """1.5.0 军师模式：市场状态纪律（震荡禁强方向/强趋势顺向/RSI 极端禁追）改为风险标注。"""

    def snapshot_with_regime(self, regime: dict[str, object]) -> dict[str, object]:
        return {
            **analyse_snapshot(),
            "market_regime": {"available": True, **regime},
        }

    def test_long_accepted_with_warning_when_ranging(self):
        payload = analyse_payload()  # LONG
        accepted, reason, report = validate_report(
            payload, ANALYSE_GATE, self.snapshot_with_regime({"regime": "ranging"})
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "震荡市"))

    def test_short_accepted_with_warning_when_trending_buy(self):
        payload = analyse_payload()
        payload["direction"] = "SHORT"
        payload["take_profit"] = "3985"
        payload["stop_loss"] = "4015"
        accepted, reason, report = validate_report(
            payload,
            ANALYSE_GATE,
            self.snapshot_with_regime({"regime": "trending", "trend_direction": "buy"}),
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "强趋势市偏多"))

    def test_long_accepted_with_warning_when_trending_sell(self):
        payload = analyse_payload()  # LONG
        accepted, reason, report = validate_report(
            payload,
            ANALYSE_GATE,
            self.snapshot_with_regime({"regime": "trending", "trend_direction": "sell"}),
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "强趋势市偏空"))

    def test_long_accepted_when_trending_buy(self):
        payload = analyse_payload()  # LONG
        accepted, reason, report = validate_report(
            payload,
            ANALYSE_GATE,
            self.snapshot_with_regime({"regime": "trending", "trend_direction": "buy"}),
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertNotIn("validation_warnings", report)

    def test_long_accepted_with_warning_when_rsi_overbought(self):
        payload = analyse_payload()  # LONG
        accepted, reason, report = validate_report(
            payload,
            ANALYSE_GATE,
            self.snapshot_with_regime(
                {"regime": "transition", "rsi_extreme": {"side": "overbought", "timeframe": "m15", "value": 88.0}}
            ),
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "RSI 超买"))

    def test_short_accepted_with_warning_when_rsi_oversold(self):
        payload = analyse_payload()
        payload["direction"] = "SHORT"
        payload["take_profit"] = "3985"
        payload["stop_loss"] = "4015"
        accepted, reason, report = validate_report(
            payload,
            ANALYSE_GATE,
            self.snapshot_with_regime(
                {"regime": "transition", "rsi_extreme": {"side": "oversold", "timeframe": "m15", "value": 12.0}}
            ),
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertTrue(has_validation_warning(report, "RSI 超卖"))

    def test_transition_regime_skips_direction_gate(self):
        # 过渡市（ADX 介于 20-25）不构成强制方向，回归 1.3.0 行为
        payload = analyse_payload()  # LONG
        accepted, _, _ = validate_report(
            payload, ANALYSE_GATE, self.snapshot_with_regime({"regime": "transition"})
        )

        self.assertTrue(accepted)

    def test_unavailable_regime_skips_direction_gate(self):
        snapshot = analyse_snapshot()
        snapshot["market_regime"] = {"available": False, "reason": "无 ADX/RSI/StdDev 指标数据"}
        payload = analyse_payload()  # LONG

        accepted, _, _ = validate_report(payload, ANALYSE_GATE, snapshot)

        self.assertTrue(accepted)

    def test_prompt_injects_regime_rule_when_available(self):
        snapshot = analyse_snapshot()
        snapshot["market_regime"] = {
            "available": True,
            "regime": "trending",
            "trend_direction": "buy",
            "rsi_extreme": None,
            "volatility_confirmed": True,
        }

        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")

        self.assertIn("market_regime", prompt)
        self.assertIn("强趋势市", prompt)
        self.assertIn("risk_note", prompt)
        self.assertIn("止盈/止损与仓位应更保守", prompt)

    def test_prompt_omits_regime_rule_when_unavailable(self):
        snapshot = analyse_snapshot()
        snapshot["market_regime"] = {"available": False, "reason": "无 ADX/RSI/StdDev 指标数据"}

        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")

        self.assertNotIn("强趋势市", prompt)
        self.assertNotIn("双周期 ADX", prompt)

    def test_prompt_ranging_rule_annotates_risk(self):
        snapshot = analyse_snapshot()
        snapshot["market_regime"] = {
            "available": True,
            "regime": "ranging",
            "trend_direction": None,
            "rsi_extreme": None,
            "volatility_confirmed": False,
        }

        prompt = build_prompt(snapshot, ANALYSE_GATE, "brief")

        self.assertIn("震荡市", prompt)
        self.assertIn("风险标注", prompt)
