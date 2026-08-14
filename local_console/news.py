"""Recent XAU/macro news context for the model, fetched from Yahoo via yfinance.

新闻在本系统里是**纯上下文层**：只用于让模型评估“预期差 / 是否已定价”，
绝不驱动闸门动作（是否禁行仍由事件日历与手动覆写决定）。为做到“少而准”：
- 仅用 XAU 专属查询集拉取；
- 关键词相关性过滤（命中才保留）；
- 24 小时新鲜度窗口 + Top N 限额 + 标题去重；
- 缓存 30 分钟，避免每次任务都访问 Yahoo。

无网络 / yfinance 失败时返回 status="unavailable"，绝不阻断任务（平行 macro.py）。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import ConsoleConfig

NEWS_LOOKBACK_HOURS = 24
NEWS_CACHE_TTL_MINUTES = 30
NEWS_LIMIT = 8
NEWS_PER_QUERY = 10
NEWS_FETCH_TIMEOUT_SECONDS = 15.0  # yfinance 无内建超时，用 daemon 线程硬切断
NEWS_QUERIES_ENV = "XAU_CONSOLE_NEWS_QUERIES"
DEFAULT_NEWS_QUERIES = (
    "黄金 gold",
    "XAU USD gold price",
    "Federal Reserve interest rates",
    "US inflation CPI PCE",
    "US dollar DXY",
)
NEWS_NOTE = (
    "近期新闻背景，仅作预期差/是否已定价的参考，"
    "不用于盘中价位或分钟级结构，不得据此反应式追单。"
)
# 相关性过滤：标题或摘要命中其一才视为与 XAU 盘面相关（去噪的核心）。
RELEVANCE_PATTERN = re.compile(
    r"黄金|gold|XAU|Fed|美联储|利率|rate|通胀|inflation|CPI|PCE|PPI|NFP|非农|"
    r"GDP|美元|USD|DXY|地缘|geopolit",
    re.IGNORECASE,
)

# 关键词 → 中文主题标签映射（优先级从上到下，先命中先取）。
_TOPIC_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Fed|美联储|FOMC|利率|rate\s*(cut|hike|decision)", re.I), "美联储/利率"),
    (re.compile(r"通胀|inflation|CPI|PCE|PPI", re.I), "通胀数据"),
    (re.compile(r"NFP|非农|Payroll|就业", re.I), "就业数据"),
    (re.compile(r"GDP|经济", re.I), "经济增长"),
    (re.compile(r"美元|USD|DXY|dollar", re.I), "美元动态"),
    (re.compile(r"地缘|geopolit|战争|冲突|制裁", re.I), "地缘风险"),
    (re.compile(r"黄金|gold|XAU|金价", re.I), "黄金走势"),
]


def _topic_tag(article: dict[str, Any]) -> str:
    """根据标题/摘要命中的关键词返回中文主题标签。"""
    text = f"{article.get('title', '')} {article.get('summary', '')}"
    for pattern, label in _TOPIC_RULES:
        if pattern.search(text):
            return label
    return "市场动态"


def _news_queries() -> tuple[str, ...]:
    raw = os.environ.get(NEWS_QUERIES_ENV)
    if not raw:
        return DEFAULT_NEWS_QUERIES
    queries = tuple(item.strip() for item in raw.split(",") if item.strip())
    return queries or DEFAULT_NEWS_QUERIES


def _read_cache(path, now: datetime) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    fetched = payload.get("fetched_at")
    if not isinstance(fetched, str):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched).astimezone(timezone.utc)
    except ValueError:
        return None
    if now.astimezone(timezone.utc) - fetched_at > timedelta(minutes=NEWS_CACHE_TTL_MINUTES):
        return None
    return payload


def _write_cache(path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 缓存失败不影响主流程


def _extract_article(article: dict[str, Any]) -> dict[str, Any] | None:
    """兼容 yfinance 的 nested(content)/flat 两形态，抽取标题/来源/时间/摘要/链接。"""
    content = article.get("content")
    if isinstance(content, dict):
        title = str(content.get("title") or "").strip()
        summary = str(content.get("summary") or "").strip()
        provider = content.get("provider") or {}
        publisher = str(provider.get("displayName") or "Unknown") if isinstance(provider, dict) else "Unknown"
        url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        link = str(url_obj.get("url") or "") if isinstance(url_obj, dict) else ""
        pub_raw = content.get("pubDate")
        pub_date = None
        if isinstance(pub_raw, str) and pub_raw:
            try:
                pub_date = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
            except ValueError:
                pub_date = None
    else:
        title = str(article.get("title") or "").strip()
        summary = str(article.get("summary") or "").strip()
        publisher = str(article.get("publisher") or "Unknown")
        link = str(article.get("link") or "")
        ts = article.get("providerPublishTime")
        pub_date = None
        if isinstance(ts, (int, float)):
            try:
                pub_date = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                pub_date = None
    if not title:
        return None
    return {
        "title": title,
        "summary": summary,
        "publisher": publisher,
        "link": link,
        "pub_date": pub_date,
    }


def _is_relevant(article: dict[str, Any]) -> bool:
    text = f"{article.get('title', '')} {article.get('summary', '')}"
    return bool(RELEVANCE_PATTERN.search(text))


def _collect_articles(queries: tuple[str, ...], now: datetime) -> list[dict[str, Any]]:
    import yfinance as yf  # lazy import：避免在控制台启动时引入重量级依赖

    window_start = now - timedelta(hours=NEWS_LOOKBACK_HOURS)
    seen: set[str] = set()
    collected: list[dict[str, Any]] = []
    for query in queries:
        search = yf.Search(query=query, news_count=NEWS_PER_QUERY, enable_fuzzy_query=True)
        for raw in getattr(search, "news", None) or []:
            if not isinstance(raw, dict):
                continue
            article = _extract_article(raw)
            if article is None or not _is_relevant(article):
                continue
            pub_date = article["pub_date"]
            # 无时间或超出新鲜度窗口的一律不纳入（无法证明不是旧闻/未来新闻）。
            if pub_date is None or pub_date.astimezone(timezone.utc) < window_start:
                continue
            if article["title"] in seen:
                continue
            seen.add(article["title"])
            collected.append(article)
        if len(collected) >= NEWS_LIMIT:
            break
    collected.sort(key=lambda item: item["pub_date"].astimezone(timezone.utc), reverse=True)
    return collected[:NEWS_LIMIT]


def _collect_articles_with_timeout(
    queries: tuple[str, ...],
    reference_now: datetime,
) -> list[dict[str, Any]] | None:
    """在 daemon 线程中执行 _collect_articles，超时返回 None（不抛异常）。"""
    timeout = NEWS_FETCH_TIMEOUT_SECONDS  # 运行时读取，允许测试 patch
    result: dict[str, Any] = {"articles": None, "error": None}

    def _run() -> None:
        try:
            result["articles"] = _collect_articles(queries, reference_now)
        except Exception as error:  # noqa: BLE001
            result["error"] = error

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        return None  # 超时：daemon 线程随进程退出，不阻塞主流程
    if result["error"] is not None:
        raise result["error"]
    return result["articles"] or []


def fetch_news_context(config: ConsoleConfig, now: datetime | None = None) -> dict[str, Any]:
    """返回近期 XAU/宏观新闻背景层；never raises。"""
    reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cached = _read_cache(config.news_cache_path, reference_now)
    if cached is not None:
        return cached
    try:
        articles = _collect_articles_with_timeout(_news_queries(), reference_now)
    except Exception as error:  # 无网络 / 被限流 / yfinance 异常 → 静默降级
        return {"status": "unavailable", "reason": f"新闻获取失败（{str(error)[:160]}）"}
    if articles is None:  # 超时
        return {"status": "unavailable", "reason": "新闻获取超时"}
    items = [
        {
            "title": article["title"],
            "topic": _topic_tag(article),
            "publisher": article["publisher"],
            "utc": article["pub_date"].astimezone(timezone.utc).isoformat(),
            "summary": article["summary"],
            "link": article["link"],
        }
        for article in articles
    ]
    payload: dict[str, Any] = {
        "status": "ok",
        "as_of": reference_now.isoformat(),
        "frequency": "recent",
        "note": NEWS_NOTE,
        "items": items,
        "fetched_at": reference_now.isoformat(),
    }
    _write_cache(config.news_cache_path, payload)
    return payload
