from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path

from local_console.config import ConsoleConfig
from local_console.server import make_server
from local_console.service import ConsoleService


def fake_snapshot(_config: ConsoleConfig, _job_id: str) -> dict[str, object]:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "identity_match": True,
        "symbol": "XAUUSD",
        "bid": 4000.0,
        "ask": 4000.1,
    }


def fake_brief(_config: ConsoleConfig, _kind: str, _snapshot: dict[str, object], _gate: object, _mode: str = "scalp") -> object:
    return {
        "action": "ANALYSE",
        "source_ids": ["mt5_snapshot", "verified_event_context"],
        "summary": "这是测试报告。",
        "invalidation": "后续快照会替代它。",
        "next_observation": "M1 收盘后刷新。",
        "evidence_fields": ["bid", "symbol"],
        "direction": "LONG",
        "entry_zone": "3995-4005",
        "take_profit": "4015",
        "stop_loss": "3985",
        "risk_note": "测试环境风险提示。",
        "suggestions": ["若回踩 3995-4005 不破可入场", "突破后关注量能确认"],
        "scenarios": ["若跌破 3985 立即离场", "若冲高遇阻减仓一半"],
        "avoid": ["不追突破", "不在数据窗口前重仓"],
    }


def make_test_service(config: ConsoleConfig, **overrides) -> ConsoleService:
    """测试服务：全部外部依赖注入 fake，避免真实网络/MT5。"""
    kwargs = {
        "snapshot_runner": fake_snapshot,
        "event_loader": lambda _path: {"status": "verified_clear"},
        "brief_runner": fake_brief,
        "tick_runner": lambda _c, _j: {"available": False, "reason": "测试环境无 MT5"},
        "macro_runner": lambda _c: {"status": "unavailable", "reason": "测试环境无 FRED"},
        "news_runner": lambda _c: {"status": "unavailable", "reason": "测试环境无网络"},
        "iv_runner": lambda _c: {"status": "unavailable", "reason": "测试环境无 IV 数据源"},
        **overrides,
    }
    return ConsoleService(config, **kwargs)


class ServerTests(unittest.TestCase):
    def test_server_refuses_non_localhost_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ConsoleConfig(
                repo_root=root,
                state_dir=root / "runtime",
                mt5_python=root / "python.exe",
                mt5_snapshot_script=root / "snapshot.py",
                backend_url="https://example.invalid/v1",
                quick_model="quick",
                deep_model="deep",
                host="0.0.0.0",
            )

            with self.assertRaisesRegex(ValueError, "127.0.0.1"):
                make_server(config)

    def test_job_api_returns_a_queued_job_with_durable_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ConsoleConfig(
                repo_root=root,
                state_dir=root / "runtime",
                mt5_python=root / "python.exe",
                mt5_snapshot_script=root / "snapshot.py",
                backend_url="https://example.invalid/v1",
                quick_model="quick",
                deep_model="deep",
                port=0,
            )
            service = make_test_service(config)
            server = make_server(config, service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                created = request_json(port, "POST", "/api/jobs", {"kind": "brief"})
                current = request_json(port, "GET", f"/api/jobs/{created['id']}")
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
                service.close()

        self.assertEqual("QUEUED", created["stage"])
        self.assertIn(current["stage"], {"QUEUED", "SNAPSHOT", "GATE", "MODEL", "VALIDATE", "COMPLETE"})
        self.assertIn("events", current)
        self.assertIsInstance(current["elapsed_seconds"], float)

    def test_auto_endpoint_toggles_and_status_reflects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ConsoleConfig(
                repo_root=root,
                state_dir=root / "runtime",
                mt5_python=root / "python.exe",
                mt5_snapshot_script=root / "snapshot.py",
                backend_url="https://example.invalid/v1",
                quick_model="quick",
                deep_model="deep",
                port=0,
            )
            service = make_test_service(config)
            server = make_server(config, service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                initial = request_json(port, "GET", "/api/auto")
                enabled = request_json(port, "POST", "/api/auto", {"enabled": True})
                status = request_json(port, "GET", "/api/status")
                bad_status, bad_body = request_raw(port, "POST", "/api/auto", {})
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
                service.close()

        self.assertFalse(initial["enabled"])
        self.assertIn("interval_seconds", initial)
        self.assertTrue(enabled["enabled"])
        self.assertTrue(status["auto"]["enabled"])
        self.assertEqual(400, bad_status)
        self.assertIn("enabled", bad_body["error"])

    def test_mode_endpoint_reads_and_switches(self):
        """GET/POST /api/mode：默认 scalp，可切 swing，非法值 400。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ConsoleConfig(
                repo_root=root,
                state_dir=root / "runtime",
                mt5_python=root / "python.exe",
                mt5_snapshot_script=root / "snapshot.py",
                backend_url="https://example.invalid/v1",
                quick_model="quick",
                deep_model="deep",
                port=0,
            )
            service = make_test_service(config)
            server = make_server(config, service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                initial = request_json(port, "GET", "/api/mode")
                switched = request_json(port, "POST", "/api/mode", {"mode": "swing"})
                bad_status, bad_body = request_raw(port, "POST", "/api/mode", {"mode": "grid"})
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
                service.close()

        self.assertEqual("scalp", initial["mode"])
        self.assertEqual("swing", switched["mode"])
        self.assertEqual(400, bad_status)
        self.assertIn("mode", bad_body["error"])

    def test_unexpected_service_error_returns_500_json_instead_of_dropping(self):
        """处理线程遇未捕获异常时必须回 500 JSON，不得静默断开连接。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ConsoleConfig(
                repo_root=root,
                state_dir=root / "runtime",
                mt5_python=root / "python.exe",
                mt5_snapshot_script=root / "snapshot.py",
                backend_url="https://example.invalid/v1",
                quick_model="quick",
                deep_model="deep",
                port=0,
            )
            service = make_test_service(config)

            def exploding_start(_kind: str):
                raise RuntimeError("private implementation detail")

            service.start = exploding_start  # type: ignore[method-assign]
            server = make_server(config, service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                status, body = request_raw(port, "POST", "/api/jobs", {"kind": "brief"})
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
                service.close()

        self.assertEqual(500, status)
        self.assertIn("error", body)
        self.assertNotIn("private", body["error"])


def request_json(port: int, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    payload = None if body is None else json.dumps(body)
    headers = {} if payload is None else {"Content-Type": "application/json"}
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    result = json.loads(response.read().decode("utf-8"))
    connection.close()
    if response.status not in {200, 202}:
        raise AssertionError(result)
    return result


def request_raw(port: int, method: str, path: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    payload = None if body is None else json.dumps(body)
    headers = {} if payload is None else {"Content-Type": "application/json"}
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    result = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, result
