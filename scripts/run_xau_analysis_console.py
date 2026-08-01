"""Start the local XAU Analysis Console without exposing it to the network."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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


def check_llm_dependencies() -> None:
    """用错 Python 环境时在启动前给出修复指令，而不是让服务"能开但一分析就失败"。

    2026-08-01 事故：用系统 python 启动控制台，缺 langchain_core，
    每个任务在 MODEL 阶段 1 秒内全部失败且页面提示不明确。
    """
    missing = [
        package
        for package in ("langchain_core", "langchain_openai")
        if importlib.util.find_spec(package) is None
    ]
    if not missing:
        return
    venv_python = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
    print(
        "XAU Analysis Console 启动失败：当前 Python 环境缺少模型依赖（"
        + "、".join(missing)
        + "）。\n请改用项目虚拟环境启动：\n  "
        + str(venv_python)
        + " scripts/run_xau_analysis_console.py",
        file=sys.stderr,
    )
    raise SystemExit(1)


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


def acquire_instance_lock(config: ConsoleConfig) -> object | None:
    """独占状态目录锁；Windows 文件锁随句柄/进程退出自动释放。

    端口探测存在 TOCTOU 竞态（两个实例同一秒启动都探测不到对方），
    allow_reuse_address 只挡住第二个端口绑定、挡不住第二个进程继续读写
    同一 jobs 目录——2026-08-01 双实例互踩导致任务永久卡 QUEUED 的根因。
    """
    import msvcrt

    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_dir / "console.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        handle.write(f"{os.getpid()}\n")  # a+ 模式写入从文件末尾追加
        handle.flush()
        return handle
    except OSError:
        handle.close()
        return None


def main(argv: list[str] | None = None) -> int:
    check_llm_dependencies()
    host, port = launch_arguments(argv or [])
    config = replace(ConsoleConfig.from_repo(), host=host, port=port)
    lock = acquire_instance_lock(config)
    if lock is None:
        existing_url = find_existing_console_url(host, port)
        if existing_url:
            print(f"XAU Analysis Console: {existing_url}")
            webbrowser.open(existing_url)
            return 0
        print("XAU Analysis Console 已在运行，但本机探测失败；请先停止现有实例", file=sys.stderr)
        return 1
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
        lock.close()  # 句柄关闭即释放文件锁


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
