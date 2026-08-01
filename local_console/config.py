"""Local-only configuration for the XAU analysis console."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_MT5_PYTHON = Path(r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe")
DEFAULT_MT5_SNAPSHOT_SCRIPT = Path(r"D:\XAU\scripts\mt5_xau_market_context_once.py")
# Cerberus EA 运行态（接口 B：闸门前向对齐，只读消费）。路径是本机 MT5 终端的
# MQL5/Files 目录；EA 未运行时文件缺失或陈旧，读取层按不可用静默忽略。
DEFAULT_EA_STATUS_FILE = Path(
    r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal"
    r"\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\ng_status.json"
)


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
    port: int = 8767
    # 自主调度：默认关闭（保守取向，由交易员显式开启）；开启后按固定节奏采样，
    # 默认 900 秒（15 分钟）对齐看 M15/H1 收盘的交易节奏，而非每分钟刷简报。
    auto_interval_seconds: int = 900
    auto_enabled_default: bool = False
    # EA 风险态接入（接口 B）：默认 None = 关闭（直接构造配置时保守关闭）；
    # from_repo 才按环境变量/默认路径开启。陈旧阈值默认 120 秒（EA 约 30 秒写一次）。
    ea_status_path: Path | None = None
    ea_status_max_age_seconds: float = 120.0

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
            auto_interval_seconds=int(os.environ.get("XAU_CONSOLE_AUTO_INTERVAL_SECONDS", "900")),
            auto_enabled_default=os.environ.get("XAU_CONSOLE_AUTO_ENABLED", "").strip().lower()
            in ("1", "true", "yes", "on"),
            ea_status_path=_ea_status_path_from_env(),
            ea_status_max_age_seconds=float(
                os.environ.get("XAU_CONSOLE_EA_STATUS_MAX_AGE", "120")
            ),
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

    @property
    def calendar_path(self) -> Path:
        return self.state_dir / "calendar.json"

    @property
    def macro_cache_path(self) -> Path:
        return self.state_dir / "macro_cache.json"

    @property
    def news_cache_path(self) -> Path:
        return self.state_dir / "news_cache.json"

    @property
    def tick_script_path(self) -> Path:
        return self.repo_root / "scripts" / "mt5_xau_tick_health_once.py"

    @property
    def combined_script_path(self) -> Path:
        return self.repo_root / "scripts" / "mt5_xau_snapshot_with_ticks_once.py"

    @property
    def review_script_path(self) -> Path:
        return self.repo_root / "scripts" / "mt5_xau_review_bars_once.py"

    @property
    def runlog_path(self) -> Path:
        return self.state_dir / "logs" / "run_log.jsonl"


def _ea_status_path_from_env() -> Path | None:
    """EA 状态文件路径：环境变量显式置空 = 关闭接入；未设置 = 本机默认终端路径。"""
    raw = os.environ.get("XAU_CONSOLE_EA_STATUS_FILE")
    if raw is not None and raw.strip() == "":
        return None
    return Path(raw) if raw else DEFAULT_EA_STATUS_FILE
