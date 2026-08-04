"""Gold implied-volatility layer via the GLD options chain (yfinance).

IV 维度（期权隐含波动率）是市场用真金白银表达的"未来波动预期"：
- ATM IV：平值期权隐含波动率（波动预期温度计）；
- Skew：隐含偏斜（25-delta 看跌 − 看涨 IV，机构下行保护需求，黄金常态为下行偏斜）；
- IV vs HV：同期限 ATM IV 与历史波动率对比（VRP 思想：IV² − RV² 同期限匹配）；
- IV Rank：当前 ATM IV 在近 N 日窗口中的百分位（由每日缓存累积，初始阶段标记积累中）；
- 期限结构：多个到期日的 ATM IV 曲线斜率（近端 vs 远端波动预期）。

数据源：GLD ETF 期权链（yfinance，免费、稳定）。GLD 是现货黄金的最优公开期权代理，
其 IV 结构与 COMEX GC 期权高度相关（同一标的资产、同一做市商群体）。
GLD 期权无单独每日 IV 历史序列，故 IV Rank 依赖本模块每日缓存累积。

方法论（吸收自开源期权分析项目 options-eye / vol-surface-opt-trans）：
- 只用 OTM + ATM 合约参与拟合（ITM IV 因提前行权价值不稳定，options-eye 纪律）；
- 对每个到期日拟合 Gatheral raw SVI 曲面（无套利约束：b>0、σ>0、蝴蝶约束），
  ATM IV 取拟合曲面在 k=0 的值（比单档最近 strike 更抗噪）；
- SVI 的 rho 参数即隐含偏斜方向指标（rho<0 = 下行偏斜）；
- 期限结构斜率 = 短端（≤30d）ATM IV 与长端（≥60d）ATM IV 之差。

与 macro.py/news.py 同一纪律：任何失败都返回 status="unavailable"，绝不阻断任务。
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from .config import ConsoleConfig

IV_CACHE_TTL_HOURS = 6
IV_RANK_WINDOW_DAYS = 60
# ATM IV 期限结构分桶（天）：短端 / 长端
IV_SHORT_BUCKET_DAYS = 30
IV_LONG_BUCKET_DAYS = 60
IV_TARGET_DAYS = 40  # 主 IV 值取最接近 40 天的到期日
IV_FALLBACK_MAX_DAYS = 90
IV_CHAIN_ATTEMPTS = 3
# IV 与 HV 比较的判定阈值（绝对值差，单位：波动率百分点）。
IV_HV_GAP_SIGNIFICANT = 0.03
# SVI 拟合：最少 OTM+ATM 数据点数
SVI_MIN_POINTS = 6
# 无套利约束（Gatheral & Jacquier 2014）：b(1+|ρ|) ≤ 4/σ²
SVI_BUTTERFLY_MARGIN = 1.0

IV_NOTE = "IV 为 GLD 期权链推导的波动预期（期权市场定价），只描述波动幅度预期，不提供方向。"


def _annualized_hv(closes: list[float], window: int) -> float | None:
    """滚动窗口年化历史波动率；样本不足返回 None。

    2026-08-04 修复：浮点累加误差可使 var 为极小负数（如 -1e-18），
    math.sqrt(负) 返回 NaN——NaN 经 json.dumps 序列化为非法 JSON，
    浏览器 JSON.parse 抛 "Unexpected token N"，导致前端状态读取全挂。
    """
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - window, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var < 0:
        return 0.0  # 浮点误差：视为零波动
    return math.sqrt(var) * math.sqrt(252)


def _svi_total_var(k: np.ndarray, a: float, b: float, rho: float, m: float, sigma: float) -> np.ndarray:
    """Gatheral raw SVI：w(k) = a + b(ρ(k−m) + √((k−m)² + σ²))。"""
    d = np.sqrt((k - m) ** 2 + sigma**2)
    return a + b * (rho * (k - m) + d)


def _golden(fn, lo: float, hi: float, iters: int = 30) -> float:
    """一维黄金分割最小化。"""
    phi = (math.sqrt(5.0) - 1.0) / 2.0
    x1 = hi - phi * (hi - lo)
    x2 = lo + phi * (hi - lo)
    f1, f2 = fn(x1), fn(x2)
    for _ in range(iters):
        if f1 < f2:
            hi, x2, f2 = x2, x1, f1
            x1 = hi - phi * (hi - lo)
            f1 = fn(x1)
        else:
            lo, x1, f1 = x1, x2, f2
            x2 = lo + phi * (hi - lo)
            f2 = fn(x2)
    return (lo + hi) / 2.0


def _svi_fit_slice(
    log_moneyness: np.ndarray,
    ivs: np.ndarray,
    weights: np.ndarray,
) -> dict[str, Any] | None:
    """numpy 手写最小二乘拟合 raw SVI 到单到期日切片。

    参数化：w(k) = a + b(ρ(k−m) + √((k−m)²+σ²))，总方差 w = IV²·τ。
    scipy 不可用时：网格初值 + 坐标下降（黄金分割单参数搜索）。
    返回拟合参数 + ATM IV（k=0）；拟合失败或无套利违反返回 None（调用方回退）。
    """
    n = len(log_moneyness)
    if n < SVI_MIN_POINTS:
        return None
    w_mkt = ivs.astype(float) ** 2
    # 稳健初值：用近 ATM（|k|<0.2）的中位数，避免深 OTM 噪声拉偏网格
    near_atm = w_mkt[np.abs(log_moneyness.astype(float)) < 0.2]
    w_ref = float(np.median(near_atm)) if len(near_atm) >= 3 else float(np.median(w_mkt))
    if not np.isfinite(w_ref) or w_ref <= 0:
        return None
    weights = np.asarray(weights, dtype=float)
    weights = np.maximum(weights, 1e-6)
    k = log_moneyness.astype(float)

    def _objective(params: tuple[float, float, float, float, float]) -> float:
        a, b, rho, m, sigma = params
        if b <= 0 or sigma <= 0 or abs(rho) >= 1.0:
            return 1e12
        w_fit = _svi_total_var(k, a, b, rho, m, sigma)
        if not np.all(np.isfinite(w_fit)) or np.any(w_fit <= 0):
            return 1e12
        err = w_fit - w_mkt
        return float(np.sqrt(np.average(err**2, weights=weights)))

    best: tuple[float, tuple[float, float, float, float, float]] | None = None
    # 初值网格：a 从 w 均值出发，ρ 扫描 ±0.6，σ 扫描 0.1-0.3
    for rho0 in (-0.6, -0.3, 0.0, 0.3):
        for sigma0 in (0.1, 0.2, 0.3):
            params = (w_ref * 0.5, w_ref * 1.5, rho0, 0.0, sigma0)
            val = _objective(params)
            if best is None or val < best[0]:
                best = (val, params)
    if best is None:
        return None

    def _coord_descent(start: tuple[float, float, float, float, float], rounds: int = 50) -> tuple[float, float, float, float, float]:
        a, b, rho, m, sigma = start
        for _ in range(rounds):
            # 闭包在 _golden 调用期间同步执行（无延迟求值），捕获当前值安全；B023 为误报。
            a = _golden(lambda x: _objective((x, b, rho, m, sigma)), max(1e-6, w_ref * 0.05), w_ref * 2.0)  # noqa: B023
            b = _golden(lambda x: _objective((a, x, rho, m, sigma)), 1e-6, w_ref * 4.0)  # noqa: B023
            rho = _golden(lambda x: _objective((a, b, x, m, sigma)), -0.9, 0.9)  # noqa: B023
            m = _golden(lambda x: _objective((a, b, rho, x, sigma)), -0.6, 0.6)  # noqa: B023
            sigma = _golden(lambda x: _objective((a, b, rho, m, x)), 1e-4, 0.5)  # noqa: B023
        return (a, b, rho, m, sigma)

    a, b, rho, m, sigma = _coord_descent(best[1])
    # 无套利约束检查（Gatheral & Jacquier 2014）
    if b <= 0 or sigma <= 0 or b * (1.0 + abs(rho)) > 4.0 / (sigma**2) * SVI_BUTTERFLY_MARGIN:
        return None
    # ATM IV：k=0 处的拟合总方差
    w_atm = float(_svi_total_var(np.array([0.0]), a, b, rho, m, sigma)[0])
    if w_atm <= 0 or not np.isfinite(w_atm):
        return None
    atm_iv = math.sqrt(w_atm)
    w_fit = _svi_total_var(k, a, b, rho, m, sigma)
    rmse = float(np.sqrt(np.average((np.sqrt(np.maximum(w_fit, 1e-12)) - ivs) ** 2, weights=weights)))
    # 拟合质量门槛：RMSE > 12% IV 视为拟合失败（深 OTM 噪声主导），调用方回退最近 strike
    if rmse > 0.12:
        return None
    return {
        "atm_iv": round(atm_iv, 4),
        "rho": round(rho, 4),  # rho<0 = 下行偏斜
        "rmse": round(rmse, 4),
        "params": [round(v, 5) for v in (a, b, rho, m, sigma)],
    }


def _extract_slice_iv(
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
    spot: float,
) -> dict[str, Any] | None:
    """单到期日：合并 OTM call + OTM put 的 log-moneyness/IV 拟合 SVI。

    返回 ATM IV（拟合）+ rho（偏斜）。数据不足时回退最近 strike 的 straddle 均值。
    """
    if not calls or not puts:
        return None
    # 只用 OTM + ATM 合约（ITM 的 IV 不稳定，options-eye 纪律）
    k_vals: list[float] = []
    iv_vals: list[float] = []
    for r in calls + puts:
        k = math.log(r["strike"] / spot)
        if not math.isfinite(k) or abs(k) > 0.35:  # 剔除过深 OTM（报价噪声大）
            continue
        k_vals.append(k)
        iv_vals.append(r["impliedVolatility"])
    if len(k_vals) < SVI_MIN_POINTS:
        return None

    fit = _svi_fit_slice(
        np.asarray(k_vals, dtype=float),
        np.asarray(iv_vals, dtype=float),
        np.ones(len(k_vals), dtype=float),
    )
    if fit is not None:
        return fit
    # 回退：最近 strike straddle 均值
    strikes = sorted({r["strike"] for r in calls})
    atm_strike = min(strikes, key=lambda s: abs(s - spot))
    call_iv = next((r["impliedVolatility"] for r in calls if r["strike"] == atm_strike), None)
    put_iv = next((r["impliedVolatility"] for r in puts if r["strike"] == atm_strike), None)
    if call_iv is None or put_iv is None:
        return None
    return {"atm_iv": round((call_iv + put_iv) / 2.0, 4), "rho": None, "rmse": None, "params": None}


def _skew(fit: dict[str, Any], calls: list[dict[str, Any]], puts: list[dict[str, Any]], spot: float) -> float | None:
    """偏斜：25-delta 近似（1σ 货币宽度）put − call IV 差。

    正 = 下行偏斜（机构买下行保护，黄金常态）；负 = 上行偏斜。
    优先用 1σ 货币宽度的实际报价差（业务语义直接），SVI rho 仅作方向交叉验证。
    """
    atm = fit.get("atm_iv")
    if not atm or atm <= 0:
        return None
    width = spot * atm * math.sqrt(30.0 / 365.0)
    near_put = min(puts, key=lambda r: abs(r["strike"] - (spot - width)))
    near_call = min(calls, key=lambda r: abs(r["strike"] - (spot + width)))
    return round(near_put["impliedVolatility"] - near_call["impliedVolatility"], 4)


def _clean_rows(df) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in df.to_dict("records"):
        iv = r.get("impliedVolatility")
        strike = r.get("strike")
        if iv is None or strike is None:
            continue
        try:
            iv_f = float(iv)
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(iv_f) or iv_f <= 0 or not math.isfinite(strike_f) or strike_f <= 0:
            continue
        out.append({"strike": strike_f, "impliedVolatility": iv_f})
    return out


def _fetch_gld_chain(config: ConsoleConfig, now: datetime) -> dict[str, Any] | None:
    """抓取 GLD 期权链：主到期日 IV + 期限结构（短端/长端 ATM IV）。"""
    try:
        import yfinance as yf  # lazy import：与 news.py 一致，避免控制台启动引入重量级依赖
    except ImportError:
        return None

    ticker = yf.Ticker("GLD")
    try:
        expirations = list(ticker.options or [])
    except Exception:
        return None
    if not expirations:
        return None

    today = now.date()
    candidates: list[tuple[int, str, int]] = []
    for exp in expirations:
        try:
            exp_date = datetime.fromisoformat(exp).date()
        except ValueError:
            continue
        delta = (exp_date - today).days
        if 20 <= delta <= IV_FALLBACK_MAX_DAYS:
            candidates.append((abs(delta - IV_TARGET_DAYS), exp, delta))
    if not candidates:
        return None
    candidates.sort()
    _, target, days_to_expiry = candidates[0]

    def _chain_rows(exp: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float] | None:
        try:
            chain = ticker.option_chain(exp)
            spot = float(ticker.fast_info.last_price)
            if spot <= 0:
                return None
            return _clean_rows(chain.calls), _clean_rows(chain.puts), spot
        except Exception:
            return None

    main: dict[str, Any] | None = None
    for _attempt in range(IV_CHAIN_ATTEMPTS):
        result = _chain_rows(target)
        if result is None:
            continue
        calls, puts, spot = result
        fit = _extract_slice_iv(calls, puts, spot)
        if fit is None:
            continue
        skew = _skew(fit, calls, puts, spot)
        main = {
            "atm_iv": fit["atm_iv"],
            "skew": skew,
            "rho": fit.get("rho"),
            "rmse": fit.get("rmse"),
            "days_to_expiry": days_to_expiry,
            "spot": round(spot, 2),
            "expiry": target,
        }
        break
    if main is None:
        return None

    # 期限结构：短端（≤30d）与长端（≥60d）ATM IV 斜率
    short_iv = long_iv = None
    for exp in expirations:
        try:
            exp_date = datetime.fromisoformat(exp).date()
        except ValueError:
            continue
        delta = (exp_date - today).days
        if delta <= 0 or delta > IV_FALLBACK_MAX_DAYS:
            continue
        result = _chain_rows(exp)
        if result is None:
            continue
        calls, puts, spot2 = result
        fit2 = _extract_slice_iv(calls, puts, spot2)
        if fit2 is None:
            continue
        if delta <= IV_SHORT_BUCKET_DAYS and short_iv is None:
            short_iv = fit2["atm_iv"]
        elif delta >= IV_LONG_BUCKET_DAYS and long_iv is None:
            long_iv = fit2["atm_iv"]
        if short_iv is not None and long_iv is not None:
            break
    term_slope: float | None = None
    if short_iv is not None and long_iv is not None:
        term_slope = round(long_iv - short_iv, 4)  # 正 = 远端波动预期更高
    main["term_slope"] = term_slope
    main["short_iv"] = short_iv
    main["long_iv"] = long_iv
    return main


def _read_rank_cache(path: Path) -> list[float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list) and all(isinstance(v, (int, float)) for v in payload):
            return [float(v) for v in payload]
    except (OSError, ValueError):
        pass
    return []


def _write_rank_cache(path: Path, values: list[float]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 缓存失败不影响主流程


def _sanitize_nan(node: Any) -> Any:
    """递归把 NaN/Infinity 替换为 None（防脏缓存/脏数据污染 JSON 响应）。

    2026-08-04 修复：旧代码曾在 hv20/hv60 缓存 NaN，Python json.dumps 默认
    输出非法 JSON（NaN），浏览器 JSON.parse 抛 "Unexpected token N"，
    前端状态读取全挂（用户"状态读取都失败"根因）。
    """
    if isinstance(node, dict):
        return {k: _sanitize_nan(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_sanitize_nan(v) for v in node]
    if isinstance(node, float) and (math.isnan(node) or math.isinf(node)):
        return None
    return node


def _read_snapshot_cache(path: Path, now: datetime) -> dict[str, Any] | None:
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
    if now.astimezone(UTC) - fetched_at > timedelta(hours=IV_CACHE_TTL_HOURS):
        return None
    cleaned = _sanitize_nan(payload)
    if cleaned.get("status") != "ok" or any(
        isinstance(v, float) and (math.isnan(v) or math.isinf(v))
        for v in (cleaned.get("atm_iv"), cleaned.get("hv20"), cleaned.get("hv60"))
        if isinstance(v, (int, float))
    ):
        return None  # 缓存含 NaN/Inf → 视为无效，重新拉取
    return cleaned


def _write_snapshot_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def fetch_iv_context(config: ConsoleConfig, now: datetime | None = None) -> dict[str, Any]:
    """返回 GLD 期权 IV 背景层；never raises。

    输出结构（status=ok 时）：
        atm_iv          平值隐含波动率（0.26 = 26%，SVI 拟合）
        skew            偏斜（SVI rho 派生，正 = 下行偏斜）
        iv_vs_hv        high / low / neutral（同期限 ATM IV 对比 HV20）
        iv_rank         当前 IV 在近 60 日缓存窗口的百分位（0-1）；样本不足为 None
        rank_samples    缓存样本数（<5 视为"数据积累中"）
        term_slope      期限结构斜率（长端 IV − 短端 IV）
        expiry          主到期日 / days_to_expiry / spot
    """
    reference_now = (now or datetime.now(UTC)).astimezone(UTC)
    cached = _read_snapshot_cache(config.iv_cache_path, reference_now)
    if cached is not None:
        return cached

    metrics = _fetch_gld_chain(config, reference_now)
    if metrics is None:
        return {"status": "unavailable", "reason": "GLD 期权链获取失败（网络或数据源不可用）"}

    # 历史波动率（GLD 日线，用于 IV vs HV——同期限匹配用 HV20）
    hv20 = hv60 = None
    try:
        import yfinance as yf

        history = yf.Ticker("GLD").history(period="1y", interval="1d")
        closes = [float(v) for v in history["Close"].tolist() if math.isfinite(float(v))]
        hv20 = _annualized_hv(closes, 20)
        hv60 = _annualized_hv(closes, 60)
    except Exception:
        pass

    # IV Rank：读历史缓存，追加今日 ATM IV，写回
    rank_values = _read_rank_cache(config.iv_rank_cache_path)
    rank_values.append(metrics["atm_iv"])
    rank_values = rank_values[-IV_RANK_WINDOW_DAYS:]
    _write_rank_cache(config.iv_rank_cache_path, rank_values)
    iv_rank: float | None = None
    if len(rank_values) >= 5:
        below = sum(1 for v in rank_values if v <= metrics["atm_iv"])
        iv_rank = round(below / len(rank_values), 3)

    iv_vs_hv: str = "neutral"
    if hv20 is not None:
        if metrics["atm_iv"] - hv20 > IV_HV_GAP_SIGNIFICANT:
            iv_vs_hv = "high"
        elif hv20 - metrics["atm_iv"] > IV_HV_GAP_SIGNIFICANT:
            iv_vs_hv = "low"

    payload: dict[str, Any] = {
        "status": "ok",
        "atm_iv": metrics["atm_iv"],
        "skew": metrics["skew"],
        "iv_vs_hv": iv_vs_hv,
        "iv_rank": iv_rank,
        "rank_samples": len(rank_values),
        "term_slope": metrics.get("term_slope"),
        "short_iv": metrics.get("short_iv"),
        "long_iv": metrics.get("long_iv"),
        "hv20": round(hv20, 4) if hv20 is not None else None,
        "hv60": round(hv60, 4) if hv60 is not None else None,
        "expiry": metrics["expiry"],
        "days_to_expiry": metrics["days_to_expiry"],
        "spot": metrics["spot"],
        "note": IV_NOTE,
        "fetched_at": reference_now.isoformat(),
    }
    _write_snapshot_cache(config.iv_cache_path, payload)
    return payload
