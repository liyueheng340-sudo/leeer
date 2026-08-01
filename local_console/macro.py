"""Daily macro background facts for XAU analysis, fetched from FRED.

黄金的中期定价主干是美元与实际利率。本模块拉取四个日频序列：
- DFII10：10Y TIPS 实际利率（黄金最核心的机会成本代理）
- DTWEXBGS：广义美元指数
- DGS10：10Y 名义收益率
- T10YIE：10Y 盈亏平衡通胀预期

这些是日频背景层数据，只用于中期背景判断，不描述盘中价位。
无 FRED_API_KEY 或请求失败时返回 status="unavailable"，绝不阻断任务。
结果缓存 6 小时，避免每次任务都访问 FRED。
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from .config import ConsoleConfig

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
REQUEST_TIMEOUT_SECONDS = 10
CACHE_TTL_HOURS = 6
OBSERVATION_COUNT = 10

# series_id -> 中文标签
MACRO_SERIES = {
    "DFII10": "10Y TIPS 实际利率",
    "DTWEXBGS": "广义美元指数",
    "DGS10": "10Y 名义收益率",
    "T10YIE": "10Y 盈亏平衡通胀预期",
}

BACKGROUND_NOTE = "日频宏观背景数据，仅用于中期背景判断，不应用于盘中价位或分钟级结构。"


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
        fetched_at = datetime.fromisoformat(fetched).astimezone(UTC)
    except ValueError:
        return None
    if now.astimezone(UTC) - fetched_at > timedelta(hours=CACHE_TTL_HOURS):
        return None
    return payload


def _write_cache(path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 缓存失败不影响主流程


def _fetch_series(series_id: str, api_key: str) -> dict[str, Any]:
    response = requests.get(
        FRED_OBSERVATIONS_URL,
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": str(OBSERVATION_COUNT),
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    observations = response.json().get("observations", [])
    points = [
        (row["date"], float(row["value"]))
        for row in observations
        if row.get("value") not in (".", None, "")
    ]
    if not points:
        raise ValueError(f"{series_id} 无有效观测值")
    points.reverse()  # 时间升序
    latest_date, latest_value = points[-1]
    reference_value = points[0][1]
    return {
        "label": MACRO_SERIES[series_id],
        "latest": latest_value,
        "date": latest_date,
        "change_recent": round(latest_value - reference_value, 4),
        "observations": len(points),
    }


def fetch_macro_background(
    config: ConsoleConfig, now: datetime | None = None
) -> dict[str, Any]:
    """Return the daily macro background layer; never raises."""
    reference_now = (now or datetime.now(UTC)).astimezone(UTC)
    cached = _read_cache(config.macro_cache_path, reference_now)
    if cached is not None:
        return cached

    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return {"status": "unavailable", "reason": "未配置 FRED_API_KEY"}

    series: dict[str, Any] = {}
    errors: list[str] = []
    # 四个序列相互独立，并行拉取，冷缓存延迟从 4×RTT 降为约 1×RTT
    with ThreadPoolExecutor(max_workers=len(MACRO_SERIES)) as pool:
        futures = {
            pool.submit(_fetch_series, series_id, api_key): series_id
            for series_id in MACRO_SERIES
        }
        for future in as_completed(futures):
            series_id = futures[future]
            try:
                series[series_id] = future.result()
            except (OSError, ValueError, KeyError) as error:
                # requests 异常体系挂在 IOError 下，OSError 一并覆盖网络与解析失败
                errors.append(f"{series_id}: {error}")
    if not series:
        return {
            "status": "unavailable",
            "reason": f"FRED 请求失败（{'; '.join(errors)[:200]}）",
        }

    as_of = max(item["date"] for item in series.values())
    payload: dict[str, Any] = {
        "status": "ok",
        "as_of": as_of,
        "frequency": "daily",
        "note": BACKGROUND_NOTE,
        "series": series,
        "fetched_at": reference_now.isoformat(),
    }
    if errors:
        payload["partial_errors"] = errors
    _write_cache(config.macro_cache_path, payload)
    return payload
