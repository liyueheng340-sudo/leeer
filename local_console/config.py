"""Local-only configuration for the XAU analysis console."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 默认 MT5 Python：优先取当前解释器（仓库 venv 已装 MetaTrader5 时直接用），
# 其次回退到 PATH 上的 python；均失败时由调用方报错。分享后不依赖本机绝对路径。
DEFAULT_MT5_PYTHON = Path(sys.executable) if sys.executable else Path("python")
# 快照脚本：仓库内 scripts/（分享自包含）。环境变量 XAU_CONSOLE_MT5_SNAPSHOT_SCRIPT
# 可覆盖为自定义路径（如用户自己的 MT5 采集脚本）。
_DEFAULT_MT5_SNAPSHOT_SCRIPT_NAME = "mt5_xau_market_context_once.py"
# Cerberus EA 运行态（接口 B：闸门前向对齐，只读消费）。路径是本机 MT5 终端的
# MQL5/Files 目录；EA 未运行时文件缺失或陈旧，读取层按不可用静默忽略。
# 本机路径不再硬编码（分享仓库不得携带他人终端路径）：默认 None = 关闭，
# 通过 XAU_CONSOLE_EA_STATUS_FILE 环境变量显式开启。
DEFAULT_EA_STATUS_FILE: Path | None = None


@dataclass(frozen=True)
class ConsoleConfig:
    repo_root: Path
    state_dir: Path
    mt5_python: Path
    mt5_snapshot_script: Path
    backend_url: str | None
    quick_model: str
    deep_model: str
    # 备用 LLM 端点（2026-08-03 双 key 冗余）：主端点失败/额度耗尽时切到备用。
    # 阿里云双区（国内 cn-beijing / 国际 ap-southeast-1）各持独立 token 套餐，
    # 一个套餐耗尽另一个仍可用。None = 不启用 fallback。
    fallback_backend_url: str | None = None
    fallback_api_key: str | None = None
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
    def from_repo(cls, repo_root: Path | None = None) -> ConsoleConfig:
        root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
        load_dotenv(root / ".env", override=False)
        state_dir = root / "data_cache" / "xau_analysis_console"
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            repo_root=root,
            state_dir=state_dir,
            mt5_python=Path(
                os.environ.get(
                    "XAU_CONSOLE_MT5_PYTHON",
                    _default_mt5_python(root),
                )
            ),
            mt5_snapshot_script=Path(
                os.environ.get(
                    "XAU_CONSOLE_MT5_SNAPSHOT_SCRIPT",
                    root / "scripts" / _DEFAULT_MT5_SNAPSHOT_SCRIPT_NAME,
                )
            ),
            backend_url=os.environ.get("TRADINGAGENTS_LLM_BACKEND_URL"),
            quick_model=os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM", "qwen3.7-max"),
            deep_model=os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM", "qwen3.8-max-preview"),
            # 备用端点：国际版 ap-southeast-1 套餐（与主端点国内版独立计费）
            fallback_backend_url=os.environ.get("XAU_CONSOLE_FALLBACK_BACKEND_URL"),
            fallback_api_key=os.environ.get("XAU_CONSOLE_FALLBACK_API_KEY"),
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
    def iv_cache_path(self) -> Path:
        return self.state_dir / "iv_cache.json"

    @property
    def iv_rank_cache_path(self) -> Path:
        return self.state_dir / "iv_rank_history.json"

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


def _default_mt5_python(repo_root: Path) -> str:
    """探测可用的 MT5 Python：优先仓库 venv，其次当前解释器，最后 PATH。

    分享后不硬编码本机绝对路径：朋友 clone 后用自己的 venv/解释器即可，
    只要该解释器装有 MetaTrader5 包（import_mt5 会给出明确报错提示安装）。
    """
    venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    for candidate in ("python", "python3"):
        found = shutil.which(candidate)
        if found:
            return found
    return sys.executable or "python"


def _ea_status_path_from_env() -> Path | None:
    """EA 状态文件路径：环境变量显式置空 = 关闭接入；未设置 = 默认关闭（None）。

    分享仓库不携带本机 MT5 终端路径（每个人终端哈希不同）；需要 EA 接入的
    用户通过 .env 的 XAU_CONSOLE_EA_STATUS_FILE 显式配置自己的路径。
    """
    raw = os.environ.get("XAU_CONSOLE_EA_STATUS_FILE")
    if raw is not None and raw.strip() == "":
        return None
    return Path(raw) if raw else DEFAULT_EA_STATUS_FILE
