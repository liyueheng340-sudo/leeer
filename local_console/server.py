"""A minimal localhost HTTP server for the XAU analysis console."""

from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import ConsoleConfig
from .jobs import JobKind
from .service import ConsoleService


class ConsoleHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, config: ConsoleConfig, service: ConsoleService):
        if config.host != "127.0.0.1":
            raise ValueError("XAU 分析控制台只能绑定 127.0.0.1")
        self.config = config
        self.service = service
        self.static_dir = Path(__file__).resolve().parent / "static"
        super().__init__((config.host, config.port), ConsoleRequestHandler)


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    server: ConsoleHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/status":
            latest = self.server.service.history()
            self._json(
                HTTPStatus.OK,
                {
                    "service": "ready",
                    "host": self.server.config.host,
                    "quick_model": self.server.config.quick_model,
                    "deep_model": self.server.config.deep_model,
                    "latest_job": latest[0].to_dict() if latest else None,
                },
            )
            return
        if path == "/api/history":
            self._json(HTTPStatus.OK, {"jobs": [record.to_dict() for record in self.server.service.history()]})
            return
        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/")
            try:
                self._json(HTTPStatus.OK, self.server.service.get(job_id).to_dict())
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "未找到任务"})
            return
        if path == "/":
            self._static("index.html")
            return
        if path.startswith("/static/"):
            self._static(path.removeprefix("/static/"))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "未找到资源"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/jobs":
            self._json(HTTPStatus.NOT_FOUND, {"error": "未找到资源"})
            return
        try:
            body = self._request_json()
            kind = body.get("kind")
            if kind not in {"brief", "deep_review"}:
                raise ValueError("任务类型必须是 brief 或 deep_review")
            record = self.server.service.start(kind)
        except (ValueError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.ACCEPTED, record.to_dict())

    def _request_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1024:
            raise ValueError("请求内容长度必须介于 1 和 1024 字节之间")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    def _static(self, relative: str) -> None:
        target = (self.server.static_dir / relative).resolve()
        if not target.is_relative_to(self.server.static_dir.resolve()) or not target.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "未找到资源"})
            return
        content = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def make_server(config: ConsoleConfig, service: ConsoleService | None = None) -> ConsoleHTTPServer:
    return ConsoleHTTPServer(config, service or ConsoleService(config))
