"""Statistical validation helpers for win-rate / edge claims (VARRD-inspired).

统计验证层（2026-08-07，借鉴 VARRD 的防过拟合守卫）：
- Wilson 置信区间：给胜率一点估计加上不确定性区间，暴露"3 单 100%"这类小样本
  假象（区间极宽 → 不可靠）。
- 显著性检验（vs 0.5 抛硬币基准）：胜率是否统计显著优于随机。
- Bonferroni 校正：多重分组比较时提高显著性门槛（防 cherry-picking / p-hacking）。
- edge decay 追踪：按时间段切分胜率，看是否随时间衰减（VARRD 的 edge decay）。

全部为纯函数（只依赖 math），无第三方依赖，可独立测试。
"""

from __future__ import annotations

import math
from typing import Any

# 95% 置信的 z 值（标准正态）。
Z_95 = 1.96
# 默认显著性水平。
ALPHA = 0.05


def wilson_interval(wins: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson 得分区间（改进的 Wald 区间，适合小样本与极端比例）。

    返回 (low, high) 均为 0~1 的胜率区间；n<=0 返回 None。
    Wilson 区间对 n 小、p 接近 0/1 时比正态近似更可靠——"3 单 100%"会给出
    很宽的区间（如 ~[0.29, 1.0]），直观暴露样本量不足。
    """
    if n <= 0:
        return None
    p = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n) / denom
    low = max(0.0, center - half)
    high = min(1.0, center + half)
    return (low, high)


def is_significant(wins: int, n: int, p0: float = 0.5, alpha: float = ALPHA) -> bool:
    """胜率是否显著高于基准 p0（双侧检验，用 Wilson 区间下界）。

    用 Wilson 区间下界 > p0 判定：若下界都高于基准，说明胜率以 95% 置信
    高于随机——不是运气。采用双侧 z（1.96）使判定更保守：3 单 100% 的
    下界 ~0.44 < 0.5 → 不显著，正确暴露小样本不可靠（VARRD"宁可保守"）。
    n 太小（区间过宽）时下界 ≤ p0 → 不显著。
    """
    interval = wilson_interval(wins, n, z=Z_95)
    if interval is None:
        return False
    return interval[0] > p0


def bonferroni_alpha(n_comparisons: int, alpha: float = ALPHA) -> float:
    """Bonferroni 校正：多重比较时降低每个检验的显著性水平。

    测了 k 个分组，每个分组要显著的 p 门槛降为 alpha/k——试得越多，越难"碰巧"
    显著（防 cherry-picking 多个维度挑好看的）。
    """
    if n_comparisons <= 0:
        return alpha
    return alpha / n_comparisons


def win_rate_with_ci(wins: int, n: int, alpha: float = ALPHA) -> dict[str, Any]:
    """胜率 + Wilson 置信区间 + 显著性结论，一次返回。

    返回：
        win_rate: float|None
        ci_low / ci_high: float|None
        significant: bool|None (None 当 n==0)
        note: 小样本提示
    """
    if n <= 0:
        return {"win_rate": None, "ci_low": None, "ci_high": None,
                "significant": None, "note": "无样本"}
    wins = max(0, min(wins, n))
    interval = wilson_interval(wins, n)
    low, high = interval if interval else (None, None)
    sig = is_significant(wins, n, alpha=alpha)
    note = ""
    if n < 30:
        note = "样本不足 30，胜率置信区间极宽，结论不可靠"
    elif n < 100:
        note = "样本 30-100，置信区间仍较宽，谨慎解读"
    else:
        note = "样本充足"
    return {
        "win_rate": round(wins / n, 3),
        "ci_low": round(low, 3) if low is not None else None,
        "ci_high": round(high, 3) if high is not None else None,
        "significant": sig,
        "note": note,
    }


def edge_decay(
    timestamps: list[str], outcomes: list[str], n_buckets: int = 3
) -> dict[str, Any]:
    """按时间段切分已判定样本，看胜率是否随"新近度"衰减（VARRD edge decay）。

    timestamps: ISO 时间串（升序），outcomes: 与之一一对应的 TP_FIRST/SL_FIRST。
    分成 n_buckets 段（按时间排序后均分），每段算胜率 + n。
    返回 {buckets: [{label, n, win_rate}], decayed: bool}。
    decayed = 最新一段胜率显著低于最老一段（差 > 0.15 且最新 n>=10 视为衰减信号）。
    """
    pairs = sorted(
        (ts, out) for ts, out in zip(timestamps, outcomes, strict=True)
        if out in ("TP_FIRST", "SL_FIRST")
    )
    if len(pairs) < 10:
        return {"buckets": [], "decayed": False, "note": "样本不足 10，无法追踪衰减"}
    buckets: list[dict[str, Any]] = []
    chunk = max(1, math.ceil(len(pairs) / n_buckets))
    for i in range(0, len(pairs), chunk):
        group = pairs[i : i + chunk]
        wins = sum(1 for _, out in group if out == "TP_FIRST")
        buckets.append({
            "label": f"第{len(buckets)+1}段",
            "n": len(group),
            "win_rate": round(wins / len(group), 3),
        })
    if len(buckets) >= 2:
        first, last = buckets[0], buckets[-1]
        decayed = (
            last["n"] >= 10
            and first["win_rate"] is not None
            and last["win_rate"] is not None
            and (first["win_rate"] - last["win_rate"]) > 0.15
        )
    else:
        decayed = False
    return {"buckets": buckets, "decayed": decayed, "note": "edge 衰减追踪（VARRD 式）"}
