from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from local_console.config import ConsoleConfig
from local_console.news import (
    NEWS_CACHE_TTL_MINUTES,
    NEWS_LIMIT,
    NEWS_NOTE,
    fetch_news_context,
)

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
RECENT_TS = int((NOW - timedelta(hours=2)).timestamp())
OLD_TS = int((NOW - timedelta(hours=30)).timestamp())


def make_config(root: Path) -> ConsoleConfig:
    return ConsoleConfig(
        repo_root=root,
        state_dir=root / "state",
        mt5_python=Path("python.exe"),
        mt5_snapshot_script=Path("snapshot.py"),
        backend_url=None,
        quick_model="quick",
        deep_model="deep",
    )


def flat_article(title: str, ts: int = RECENT_TS, publisher: str = "Reuters", summary: str = "") -> dict:
    return {
        "title": title,
        "providerPublishTime": ts,
        "publisher": publisher,
        "summary": summary,
        "link": f"https://example.com/{title[:10]}",
    }


def fake_yfinance_module(articles: list[dict]) -> ModuleType:
    """构造 fake yfinance 模块，Search 返回给定 articles。"""
    mod = ModuleType("yfinance")

    class FakeSearch:
        def __init__(self, **kwargs):
            self.news = articles

    mod.Search = FakeSearch  # type: ignore[attr-defined]
    return mod


def raising_yfinance_module() -> ModuleType:
    """构造 fake yfinance 模块，Search 抛异常模拟无网络。"""
    mod = ModuleType("yfinance")

    class FakeSearch:
        def __init__(self, **kwargs):
            raise ConnectionError("no network")

    mod.Search = FakeSearch  # type: ignore[attr-defined]
    return mod


class RelevanceFilterTests(unittest.TestCase):
    def test_relevant_articles_are_kept(self):
        articles = [
            flat_article("Gold prices surge as Fed signals rate cut"),
            flat_article("Random sports news about football"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual("ok", result["status"])
        self.assertEqual(1, len(result["items"]))
        self.assertIn("Gold", result["items"][0]["title"])

    def test_irrelevant_articles_are_filtered(self):
        articles = [flat_article("Local cat wins marathon")]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual("ok", result["status"])
        self.assertEqual(0, len(result["items"]))


class FreshnessWindowTests(unittest.TestCase):
    def test_old_articles_outside_24h_are_excluded(self):
        articles = [
            flat_article("Gold rally continues", ts=RECENT_TS),
            flat_article("Old gold news", ts=OLD_TS),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual(1, len(result["items"]))
        self.assertIn("rally", result["items"][0]["title"])

    def test_articles_without_timestamp_are_excluded(self):
        articles = [{"title": "Gold no time", "publisher": "X", "summary": "", "link": ""}]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual(0, len(result["items"]))


class CacheTests(unittest.TestCase):
    def test_fresh_cache_short_circuits_network(self):
        cached_payload = {
            "status": "ok",
            "as_of": NOW.isoformat(),
            "frequency": "recent",
            "note": NEWS_NOTE,
            "items": [{"title": "Cached gold", "publisher": "Cache", "utc": NOW.isoformat(), "summary": "", "link": ""}],
            "fetched_at": NOW.isoformat(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            config.news_cache_path.parent.mkdir(parents=True, exist_ok=True)
            config.news_cache_path.write_text(json.dumps(cached_payload), encoding="utf-8")
            # 注入会抛错的 yfinance：若命中缓存则不会调用
            with patch.dict(sys.modules, {"yfinance": raising_yfinance_module()}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual("ok", result["status"])
        self.assertEqual("Cached gold", result["items"][0]["title"])

    def test_expired_cache_refetches(self):
        old_time = NOW - timedelta(minutes=NEWS_CACHE_TTL_MINUTES + 1)
        cached_payload = {
            "status": "ok",
            "as_of": old_time.isoformat(),
            "frequency": "recent",
            "note": NEWS_NOTE,
            "items": [{"title": "Stale", "publisher": "X", "utc": old_time.isoformat(), "summary": "", "link": ""}],
            "fetched_at": old_time.isoformat(),
        }
        fresh_articles = [flat_article("Fresh gold update")]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            config.news_cache_path.parent.mkdir(parents=True, exist_ok=True)
            config.news_cache_path.write_text(json.dumps(cached_payload), encoding="utf-8")
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(fresh_articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual("Fresh gold update", result["items"][0]["title"])


class DeduplicationTests(unittest.TestCase):
    def test_duplicate_titles_are_removed(self):
        articles = [
            flat_article("Gold hits record high"),
            flat_article("Gold hits record high"),
            flat_article("Fed raises rates"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        titles = [item["title"] for item in result["items"]]
        self.assertEqual(2, len(titles))
        self.assertEqual(len(titles), len(set(titles)))


class LimitTests(unittest.TestCase):
    def test_output_capped_at_news_limit(self):
        articles = [flat_article(f"Gold news item {i}") for i in range(20)]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertLessEqual(len(result["items"]), NEWS_LIMIT)


class SilentDegradationTests(unittest.TestCase):
    def test_yfinance_error_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": raising_yfinance_module()}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual("unavailable", result["status"])
        self.assertIn("reason", result)

    def test_yfinance_import_error_returns_unavailable(self):
        """yfinance 未安装时 ImportError 也应静默降级。"""
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": None}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual("unavailable", result["status"])


class OutputStructureTests(unittest.TestCase):
    def test_ok_payload_has_required_keys(self):
        articles = [flat_article("Gold CPI data release")]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual("ok", result["status"])
        self.assertEqual("recent", result["frequency"])
        self.assertEqual(NEWS_NOTE, result["note"])
        self.assertIn("as_of", result)
        self.assertIn("fetched_at", result)
        item = result["items"][0]
        for key in ("title", "publisher", "utc", "summary", "link"):
            self.assertIn(key, item)

    def test_items_sorted_by_time_descending(self):
        t1 = int((NOW - timedelta(hours=1)).timestamp())
        t2 = int((NOW - timedelta(hours=5)).timestamp())
        t3 = int((NOW - timedelta(hours=3)).timestamp())
        articles = [
            flat_article("Gold A", ts=t2),
            flat_article("Gold B", ts=t1),
            flat_article("Gold C", ts=t3),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        times = [item["utc"] for item in result["items"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_cache_is_written_after_fetch(self):
        articles = [flat_article("Gold cache write test")]
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module(articles)}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                fetch_news_context(config, now=NOW)

            self.assertTrue(config.news_cache_path.exists())
            cached = json.loads(config.news_cache_path.read_text(encoding="utf-8"))
            self.assertEqual("ok", cached["status"])


class NestedFormatTests(unittest.TestCase):
    def test_nested_content_format_is_parsed(self):
        """yfinance nested(content) 形态也应正确解析。"""
        pub_date = (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        nested = {
            "content": {
                "title": "Fed holds rates steady",
                "summary": "The Federal Reserve kept rates unchanged.",
                "provider": {"displayName": "Bloomberg"},
                "canonicalUrl": {"url": "https://example.com/fed"},
                "pubDate": pub_date,
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch.dict(sys.modules, {"yfinance": fake_yfinance_module([nested])}), patch.dict("os.environ", {"XAU_CONSOLE_NEWS_QUERIES": "gold"}):
                result = fetch_news_context(config, now=NOW)

        self.assertEqual(1, len(result["items"]))
        self.assertEqual("Fed holds rates steady", result["items"][0]["title"])
        self.assertEqual("Bloomberg", result["items"][0]["publisher"])


class TimeoutProtectionTests(unittest.TestCase):
    """yfinance 无内建超时：守护线程 + join(timeout) 硬切断，不阻塞任务。"""

    def test_hanging_yfinance_degrades_silently(self):
        """yfinance 永久挂起时，fetch_news_context 应在超时后返回 unavailable。"""
        import time as _time

        def hanging_collect(*_args, **_kwargs):
            _time.sleep(999)  # 模拟网络无限挂起

        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with patch("local_console.news._collect_articles", side_effect=hanging_collect), patch("local_console.news.NEWS_FETCH_TIMEOUT_SECONDS", 0.3):
                started = _time.monotonic()
                result = fetch_news_context(config, now=NOW)
                elapsed = _time.monotonic() - started

        self.assertEqual("unavailable", result["status"])
        self.assertIn("超时", result["reason"])
        self.assertLess(elapsed, 5.0, "超时保护应在 0.3s 后返回，不应等待挂起的线程")
