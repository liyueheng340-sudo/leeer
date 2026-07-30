from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_console.brief import MODEL_TIMEOUT_SECONDS, request_brief, validate_report
from local_console.config import ConsoleConfig
from local_console.guard import GateResult


class BriefValidationTests(unittest.TestCase):
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
            content='{"action":"WATCH","source_ids":["mt5_snapshot"],"summary":"中文摘要","invalidation":"中文失效条件","next_observation":"中文观察条件"}'
        )
        client = MagicMock()
        client.get_llm.return_value = llm

        with patch("local_console.brief.create_llm_client", return_value=client) as factory:
            payload = request_brief(
                config,
                "brief",
                {"symbol": "XAUUSD"},
                GateResult("WATCH", True, False, "事件未核验"),
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
            content='{"action":"WATCH","source_ids":["mt5_snapshot"],"summary":"中文摘要","invalidation":"中文失效条件","next_observation":"中文观察条件"}'
        )
        client = MagicMock()
        client.get_llm.return_value = llm

        with patch("local_console.brief.create_llm_client", return_value=client) as factory:
            payload = request_brief(
                config,
                "brief",
                {"symbol": "XAUUSD"},
                GateResult("WATCH", True, False, "事件未核验"),
            )

        factory.assert_called_once_with(
            "qwen", "quick", None, timeout=MODEL_TIMEOUT_SECONDS, max_retries=0
        )
        self.assertEqual("WATCH", payload["action"])

    def test_report_with_unprovided_source_is_rejected(self):
        payload = {
            "action": "ANALYSE",
            "source_ids": ["mt5_snapshot", "Yahoo Finance"],
            "summary": "结构处于平衡状态。",
            "invalidation": "收盘离开观察区间后，该观察失效。",
            "next_observation": "等待新的快照。",
        }

        accepted, reason, report = validate_report(
            payload, GateResult("ANALYSE", True, True, "ok")
        )

        self.assertFalse(accepted)
        self.assertEqual("报告引用了未提供的数据源：Yahoo Finance", reason)
        self.assertIsNone(report)

    def test_watch_report_cannot_contain_direct_entry_instruction(self):
        payload = {
            "action": "WATCH",
            "source_ids": ["mt5_snapshot"],
            "summary": "立即买入。",
            "invalidation": "点差扩大时观察失效。",
            "next_observation": "等待事件核验。",
        }

        accepted, reason, report = validate_report(
            payload, GateResult("WATCH", True, False, "events unknown")
        )

        self.assertFalse(accepted)
        self.assertEqual("观察模式报告包含直接入场指令", reason)
        self.assertIsNone(report)

    def test_valid_watch_report_is_available_to_the_ui(self):
        payload = {
            "action": "WATCH",
            "source_ids": ["mt5_snapshot"],
            "summary": "M1 结构混合，等待确认收盘。",
            "invalidation": "下一次快照到来后当前观察失效。",
            "next_observation": "下一根 M1 收盘后刷新。",
        }

        accepted, reason, report = validate_report(
            payload, GateResult("WATCH", True, False, "events unknown")
        )

        self.assertTrue(accepted)
        self.assertEqual("报告已验收", reason)
        self.assertEqual(payload, report)

    def test_english_visible_report_is_rejected(self):
        payload = {
            "action": "WATCH",
            "source_ids": ["mt5_snapshot"],
            "summary": "Wait for a confirmed close.",
            "invalidation": "The next snapshot invalidates this observation.",
            "next_observation": "Refresh after M1 closes.",
        }

        accepted, reason, report = validate_report(
            payload, GateResult("WATCH", True, False, "事件未核验")
        )

        self.assertFalse(accepted)
        self.assertEqual("报告正文必须使用中文：summary", reason)
        self.assertIsNone(report)

    def test_english_dominated_report_with_token_chinese_is_rejected(self):
        payload = {
            "action": "WATCH",
            "source_ids": ["mt5_snapshot"],
            "summary": "中文 Wait for a confirmed close before entering the market.",
            "invalidation": "中文 The next snapshot invalidates this observation.",
            "next_observation": "中文 Refresh after the M1 candle closes.",
        }

        accepted, reason, report = validate_report(
            payload, GateResult("WATCH", True, False, "事件未核验")
        )

        self.assertFalse(accepted)
        self.assertEqual("报告正文必须使用中文：summary", reason)
        self.assertIsNone(report)

    def test_watch_gate_rejects_analyse_action(self):
        payload = {
            "action": "ANALYSE",
            "source_ids": ["mt5_snapshot"],
            "summary": "当前事件上下文未核验。",
            "invalidation": "下一次快照会替代本次观察。",
            "next_observation": "等待事件状态确认。",
        }

        accepted, reason, report = validate_report(
            payload, GateResult("WATCH", True, False, "事件未核验")
        )

        self.assertFalse(accepted)
        self.assertEqual("观察模式报告动作必须是 WATCH", reason)
        self.assertIsNone(report)
