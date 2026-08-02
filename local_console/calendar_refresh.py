"""Throttled network refresh of the event calendar into a trusted cache file."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from .calendar_parsers import CALENDAR_SCHEMA_VERSION, parse_calendar_payload
from .config import ConsoleConfig

FETCH_THROTTLE_HOURS = 3
FETCH_TIMEOUT_SECONDS = 10
# 日历源响应体上限：防异常源/错误源返回超大响应拖垮进程（2 MiB 对周历足够）。
CALENDAR_MAX_BYTES = 2 * 1024 * 1024
CALENDAR_URL_ENV = "XAU_CONSOLE_CALENDAR_URL"
# 免费公开周历（ForexFactory/faireconomy），仅取 USD 事件
DEFAULT_CALENDAR_URL = "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml"


def refresh_calendar_from_url(config: ConsoleConfig) -> bool:
    """Throttled best-effort refresh of calendar.json from the configured/default URL."""
    url = os.environ.get(CALENDAR_URL_ENV) or DEFAULT_CALENDAR_URL
    # 只允许 http/https 源，杜绝 file:// 等本地/任意协议读取（注入面收敛）。
    if urlparse(url).scheme.lower() not in {"http", "https"}:
        return False
    try:
        mtime = datetime.fromtimestamp(config.calendar_path.stat().st_mtime, UTC)
        if datetime.now(UTC) - mtime < timedelta(hours=FETCH_THROTTLE_HOURS):
            return False  # 3 小时内拉过，不重复请求
    except OSError:
        pass  # 文件不存在 → 立即拉取
    try:
        with urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            text = response.read(CALENDAR_MAX_BYTES + 1).decode("utf-8", errors="replace")
            if len(text) > CALENDAR_MAX_BYTES:
                return False  # 响应超过上限，拒绝使用
    except OSError:
        return False
    events = parse_calendar_payload(text)
    if not events:
        return False
    payload: dict[str, Any] = {
        "updated_at": datetime.now(UTC).isoformat(),
        "source": url,
        "schema_version": CALENDAR_SCHEMA_VERSION,
        "events": events,
    }
    try:
        config.calendar_path.parent.mkdir(parents=True, exist_ok=True)
        config.calendar_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        return False
    return True
