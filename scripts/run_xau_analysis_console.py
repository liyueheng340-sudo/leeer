"""Start the local XAU Analysis Console without exposing it to the network."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from dataclasses import replace
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    host, port = launch_arguments(argv or [])
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
