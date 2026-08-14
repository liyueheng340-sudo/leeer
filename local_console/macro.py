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
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .config import ConsoleConfig

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
# 免 API key 的 CSV 降级端点：无需 key 即可拉取单个序列的时序。
# 列名：observation_date,{SERIES_ID}。用作官方 API 失败或无 key 时的兜底。
FRED_CSV_URL = "https://fredgraph.stlouisfed.org/fredgraph.csv"
REQUEST_TIMEOUT_SECONDS = 10
CACHE_TTL_HOURS = 6
OBSERVATION_COUNT = 10
# CSV 免 key 端点连接抖动重试次数（实测代理环境下 ConnectionAborted 频发）。
CSV_CSV_RETRIES = 2

# series_id -> 中文标签（黄金中期定价的宏观驱动主干）
# 前 4 项为已有核心：实际利率 / 广义美元 / 名义利率 / 盈亏平衡通胀。
# 扩展项（2026-08-07）：DFF 资金成本、DGS2 短端预期、WALCL 扩表/QT、
# VIXCLS 避险需求、M2SL 流动性。WALCL（周频）与 M2SL（月频）为低频序列，
# OBSERVATION_COUNT 会自动跨更长时间窗，as_of 取 max(date)。
MACRO_SERIES = {
    "DFII10": "10Y TIPS 实际利率",
    "DTWEXBGS": "广义美元指数",
    "DGS10": "10Y 名义收益率",
    "T10YIE": "10Y 盈亏平衡通胀预期",
    "DFF": "联邦基金有效利率",
    "DGS2": "2Y 美债收益率",
    "WALCL": "美联储总资产",
    "VIXCLS": "恐慌指数 VIX",
    "M2SL": "M2 货币供应量",
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
        fetched_at = datetime.fromisoformat(fetched).astimezone(timezone.utc)
    except ValueError:
        return None
    if now.astimezone(timezone.utc) - fetched_at > timedelta(hours=CACHE_TTL_HOURS):
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


def _fetch_series_csv(series_id: str) -> dict[str, Any]:
    """经免 key CSV 端点拉取单序列，返回与官方 API 相同的结构。

    降级模式：官方 API 无 key 或失败时兜底。CSV 列名 observation_date,{SERIES_ID}，
    缺失值以 "." 标记须跳过。仅取 CSV 末尾 OBSERVATION_COUNT 条以保持窗口一致。
    fredgraph CSV 端点免费但连接易被代理掐断（实测 ConnectionAborted 10053），
    故做轻量重试（CSV_CSV_RETRIES 次 + 短退避），与官方 API 的容错对齐。
    """
    last_error: Exception | None = None
    for attempt in range(CSV_CSV_RETRIES + 1):
        try:
            response = requests.get(
                FRED_CSV_URL,
                params={"id": series_id},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            import csv
            from io import StringIO

            rows = list(csv.DictReader(StringIO(response.text)))
            points = [
                (row["observation_date"], float(row[series_id]))
                for row in rows
                if series_id in row
                and row.get(series_id) not in (".", None, "")
                and row.get("observation_date")
            ]
            if not points:
                raise ValueError(f"{series_id} 无有效观测值")
            points = points[-OBSERVATION_COUNT:]  # 与官方 limit 对齐窗口
            latest_date, latest_value = points[-1]
            reference_value = points[0][1]
            return {
                "label": MACRO_SERIES[series_id],
                "latest": latest_value,
                "date": latest_date,
                "change_recent": round(latest_value - reference_value, 4),
                "observations": len(points),
            }
        except Exception as error:
            last_error = error
            if attempt < CSV_CSV_RETRIES:
                import time

                time.sleep(1.0 + attempt)  # 短退避，容忍瞬时抖动
    raise ValueError(f"{series_id} CSV 拉取失败") from last_error


def _fetch_series_with_fallback(
    series_id: str, api_key: str | None
) -> dict[str, Any]:
    """拉取单序列：有 key 时优先官方 API，失败或无 key 时降级 CSV。

    返回与 _fetch_series 相同的结构；两种方式都失败则抛异常由调用方记录。
    """
    last_error: Exception | None = None
    if api_key:
        try:
            return _fetch_series(series_id, api_key)
        except Exception as error:  # 官方 API 失败，尝试 CSV 降级
            last_error = error
    # 无 key，或官方 API 失败：走免 key CSV 端点
    try:
        return _fetch_series_csv(series_id)
    except Exception as csv_error:
        reason = f"{csv_error}"
        if last_error is not None:
            reason = f"{last_error}; CSV 降级也失败: {csv_error}"
        raise ValueError(f"{series_id}: {reason}") from csv_error


def fetch_macro_background(
    config: ConsoleConfig, now: datetime | None = None
) -> dict[str, Any]:
    """Return the daily macro background layer; never raises."""
    reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cached = _read_cache(config.macro_cache_path, reference_now)
    if cached is not None:
        return cached

    api_key = os.environ.get("FRED_API_KEY")  # 无 key 时走 CSV 降级，不再直接 unavailable

    series: dict[str, Any] = {}
    errors: list[str] = []
    # 各序列相互独立，并行拉取（官方失败自动降级 CSV），冷缓存延迟从 N×RTT 降为约 1×RTT
    with ThreadPoolExecutor(max_workers=len(MACRO_SERIES)) as pool:
        futures = {
            pool.submit(_fetch_series_with_fallback, series_id, api_key): series_id
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
