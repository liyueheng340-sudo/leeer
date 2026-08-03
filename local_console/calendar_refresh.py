"""Throttled network refresh of the event calendar into a trusted cache file."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .calendar_parsers import CALENDAR_SCHEMA_VERSION, parse_calendar_payload
from .config import ConsoleConfig

FETCH_THROTTLE_HOURS = 3
FETCH_TIMEOUT_SECONDS = 10
# 全部源拉取失败后的退避窗口：源故障/限流时不随每个任务反复锤源
# （2026-08-03：nfs 源在连续请求下返回 429；此前 calendar.json 缺失时
#  每次任务都触发网络拉取，源一限流就反复失败且拖慢 GATE 阶段）。
FETCH_FAILURE_BACKOFF_SECONDS = 1800  # 30 分钟
# 日历源响应体上限：防异常源/错误源返回超大响应拖垮进程（2 MiB 对周历足够）。
CALENDAR_MAX_BYTES = 2 * 1024 * 1024
CALENDAR_URL_ENV = "XAU_CONSOLE_CALENDAR_URL"
# 免费公开周历（ForexFactory/faireconomy），仅取 USD 事件。
# 2026-08-03 修复：cdn-nfs 在本网络 TLS 握手即被掐断（SSL EOF），
# nfs（同源同内容，无 cdn- 前缀）可正常访问但会 429 限流（200/429 抖动）——
# 主源失败时按序回退备用源，避免事件日历层整体失效（此前 calendar.json
# 从未写入，所有任务都带"未核验"标注）。
# 2026-08-03 新增稳定备用源 finviz（calendar.ashx）：实测本网络 6/6 请求 200、
# 服务端渲染内嵌 JSON、含 importance 等级、无 API key，作为 nfs 限流时的兜底。
DEFAULT_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
CALENDAR_FALLBACK_URLS = (
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml",
    "https://finviz.com/calendar.ashx",
)

# 进程内最后一次全源失败时刻（monotonic，None = 无失败记录）；源故障时退避，避免每次任务重试。
# 不得用 0.0 作"无失败"哨兵：Windows 上 time.monotonic() 是系统开机时长，
# 开机 < 退避窗口时 `monotonic() - 0.0 < window` 恒成立，退避必然误触发，
# calendar.json 永不写入（2026-08-03 复审发现：系统开机 19 分钟时被测试抓到）。
_last_fetch_failure_at: float | None = None

# 日历源请求头：裸 urlopen（无 UA）会被多数源拒绝——finviz 403、nfs 429（2026-08-03
# 实测：带 UA 两者均 200；裸请求 finviz 403 / nfs 429）。这是日历层自 7/31 起
# 拉取失败的隐藏根因之一，此前从未被怀疑。
_CALENDAR_REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) XAU-Console/1.8.0"}


def refresh_calendar_from_url(config: ConsoleConfig) -> bool:
    """Throttled best-effort refresh of calendar.json, trying sources in order.

    源顺序：环境变量 URL（若有）→ 默认源 → 备用源；任一成功即写入并返回 True。
    全部失败返回 False（调用方按 unverified 降级，不阻断分析）。
    全源失败后进入 FETCH_FAILURE_BACKOFF_SECONDS 退避，期间直接返回 False 不碰网络。
    """
    global _last_fetch_failure_at
    primary = os.environ.get(CALENDAR_URL_ENV) or DEFAULT_CALENDAR_URL
    candidates = [primary] + [
        url for url in CALENDAR_FALLBACK_URLS if url != primary
    ]
    # 只允许 http/https 源，杜绝 file:// 等本地/任意协议读取（注入面收敛）。
    candidates = [url for url in candidates if urlparse(url).scheme.lower() in {"http", "https"}]
    if not candidates:
        return False
    # 全源失败退避：源故障/限流时不随任务反复锤源。
    if (
        _last_fetch_failure_at is not None
        and time.monotonic() - _last_fetch_failure_at < FETCH_FAILURE_BACKOFF_SECONDS
    ):
        return False
    try:
        mtime = datetime.fromtimestamp(config.calendar_path.stat().st_mtime, UTC)
        if datetime.now(UTC) - mtime < timedelta(hours=FETCH_THROTTLE_HOURS):
            return False  # 3 小时内拉过，不重复请求
    except OSError:
        pass  # 文件不存在 → 立即拉取
    for url in candidates:
        try:
            request = Request(url, headers=_CALENDAR_REQUEST_HEADERS)
            with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                text = response.read(CALENDAR_MAX_BYTES + 1).decode("utf-8", errors="replace")
            if len(text) > CALENDAR_MAX_BYTES:
                continue  # 响应超过上限，尝试下一个源
        except OSError:
            continue  # 当前源不可达（如 TLS 被掐断/403/429），尝试下一个源
        events = parse_calendar_payload(text)
        if events is None:
            continue  # 格式无法识别（非 JSON/ICS/FF-XML），尝试下一个源
        # 注意：events 为空列表 ≠ 拉取失败——本周无 USD 事件（休市周/安静周）是合法
        # 状态，照常写入空日历；evaluate_calendar 对空日历判 verified_clear，
        # 3 小时节流随之生效，避免每次任务都重新拉取（2026-08-03 修复：
        # 此前 `if not events` 把空日历当失败，calendar.json 永不存在，
        # 每个任务都锤源拉取，源一限流就反复失败）。
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
        _last_fetch_failure_at = None  # 成功即清除失败标记
        return True
    _last_fetch_failure_at = time.monotonic()  # 全源失败：记录退避起点
    return False
