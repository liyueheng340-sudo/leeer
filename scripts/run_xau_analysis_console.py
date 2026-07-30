"""Start the local XAU Analysis Console without exposing it to the network."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from dataclasses import replace
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_console.config import ConsoleConfig
from local_console.server import make_server


def launch_arguments(argv: list[str]) -> tuple[str, int]:
    parser = argparse.ArgumentParser(description="Start the local XAU Analysis Console.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8767, type=int)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        raise SystemExit("XAU Analysis Console only serves 127.0.0.1")
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    return args.host, args.port


def find_existing_console_url(host: str, port: int) -> str | None:
    url = f"http://{host}:{port}/api/status"
    try:
        with urlopen(url, timeout=0.2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("service") == "ready" and payload.get("host") == host:
        return f"http://{host}:{port}"
    return None


def main(argv: list[str] | None = None) -> int:
    host, port = launch_arguments(argv or [])
    existing_url = find_existing_console_url(host, port)
    if existing_url:
        print(f"XAU Analysis Console: {existing_url}")
        webbrowser.open(existing_url)
        return 0
    config = replace(ConsoleConfig.from_repo(), host=host, port=port)
    server = make_server(config)
    url = f"http://{host}:{server.server_address[1]}"
    print(f"XAU Analysis Console: {url}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        server.service.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
