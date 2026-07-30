"""Local-only configuration for the XAU analysis console."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_MT5_PYTHON = Path(r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe")
DEFAULT_MT5_SNAPSHOT_SCRIPT = Path(r"D:\XAU\scripts\mt5_xau_market_context_once.py")


@dataclass(frozen=True)
class ConsoleConfig:
    repo_root: Path
    state_dir: Path
    mt5_python: Path
    mt5_snapshot_script: Path
    backend_url: str | None
    quick_model: str
    deep_model: str
    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def from_repo(cls, repo_root: Path | None = None) -> "ConsoleConfig":
        root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
        load_dotenv(root / ".env", override=False)
        state_dir = root / "data_cache" / "xau_analysis_console"
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            repo_root=root,
            state_dir=state_dir,
            mt5_python=Path(os.environ.get("XAU_CONSOLE_MT5_PYTHON", DEFAULT_MT5_PYTHON)),
            mt5_snapshot_script=Path(
                os.environ.get("XAU_CONSOLE_MT5_SNAPSHOT_SCRIPT", DEFAULT_MT5_SNAPSHOT_SCRIPT)
            ),
            backend_url=os.environ.get("TRADINGAGENTS_LLM_BACKEND_URL"),
            quick_model=os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM", "qwen3.7-max"),
            deep_model=os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM", "qwen3.8-max-preview"),
        )

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def event_context_path(self) -> Path:
        return self.state_dir / "event_context.json"

