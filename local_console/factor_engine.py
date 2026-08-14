"""统计因子引擎 (factor_engine.py)
====================================
基于 9 个月真实 tick 因子挖掘验证的稳定因子 (前后段交叉验证同号):
  - mom_6          : 6根M15动量 (负相关: 涨过头会回落)
  - close_pos      : 收盘在窗口中的位置 (负相关: 高位收盘会回落)
  - kurtosis       : 窗口分布峰度 (正相关: 极端波动聚集后延续)
  - reversion      : 偏离中位数的回归压力 (正相关: 偏离大→回归)
  - median_dist    : 收盘距中位数距离 (负相关)

用法: 由 snapshot 的 m15 收盘序列计算, 输出因子状态供 prompt_rules 注入。

注意: 这些因子是"微弱统计优势" (IC 0.02-0.05), 用于辅助判断,
不构成单独交易信号。ADX/RSI 等传统指标已证伪, 本引擎为其补充。
"""

from __future__ import annotations

# 窗口: 24根M15 = 6小时 (与因子挖掘一致)
WINDOW = 24
# 动量窗口
MOM_BARS = 6

# 因子方向 (正相关=1, 负相关=-1)
FACTOR_DIRECTION = {
    "mom_6": -1,        # 动量↑ → 未来↓ (反转)
    "close_pos": -1,    # 高位收盘 → 未来↓
    "kurtosis": 1,      # 峰度↑ → 未来↑
    "reversion": 1,     # 偏离↑ → 回归↑ (从低位回归)
    "median_dist": -1,  # 距中位远 → 未来↓
    "reoccurrence": 1,  # 重复价位占比↑ → 未来↑ (吸筹/粘滞, tsfresh验证)
}


def _safe_std(x: list[float]) -> float:
    n = len(x)
    if n < 2:
        return 0.0
    mean = sum(x) / n
    var = sum((v - mean) ** 2 for v in x) / (n - 1)
    return var ** 0.5


def compute_factors(closes: list[float]) -> dict[str, object]:
    """输入 M15 收盘序列(至少 WINDOW+1 根), 返回因子状态。"""
    n = len(closes)
    if n < WINDOW + 1:
        return {"available": False, "reason": "insufficient bars", "factors": {}}

    win = closes[n - WINDOW:]  # 最近24根
    mean = sum(win) / WINDOW
    std = _safe_std(win)

    # mom_6: 最近6根净变化
    mom_6 = closes[-1] - closes[-1 - MOM_BARS]

    # close_pos: 收盘在窗口中的位置 [0,1]
    hi = max(win)
    lo = min(win)
    close_pos = (closes[-1] - lo) / (hi - lo) if hi > lo else 0.5

    # kurtosis: 峰度
    kurt = 0.0
    if std > 1e-9:
        kurt = sum((v - mean) ** 4 for v in win) / WINDOW / (std ** 4) - 3.0

    # reversion: 偏离中位数 (中位在下则正=向上回归压力)
    sorted_win = sorted(win)
    median = sorted_win[WINDOW // 2]
    reversion = (median - closes[-1]) / (std + 1e-9)

    # median_dist: 收盘距中位数距离
    median_dist = (closes[-1] - median) / (std + 1e-9)

    # reoccurrence: 重复价位占比 (tsfresh验证: 重复价位多→后续涨)
    # 价格取整到 tick 精度(0.01), 统计唯一价位数 / 总价位数
    tick = 0.01
    snapped = [round(v / tick) for v in win]
    unique_count = len(set(snapped))
    reoccurrence = 1.0 - unique_count / WINDOW   # 0=全独立, 1=全重复

    raw = {
        "mom_6": mom_6,
        "close_pos": close_pos,
        "kurtosis": kurt,
        "reversion": reversion,
        "median_dist": median_dist,
        "reoccurrence": reoccurrence,
    }

    # 归一化到信号强度 [-1, 1]
    signals = {}
    # mom_6: 相对波动归一
    mom_norm = mom_6 / (std + 1e-9)
    signals["mom_6"] = _clip(mom_norm * FACTOR_DIRECTION["mom_6"])

    # close_pos: 直接 0~1 转 -1~1, 方向负
    signals["close_pos"] = _clip((close_pos - 0.5) * 2.0 * FACTOR_DIRECTION["close_pos"])

    # kurtosis: 峰度 >2 视为聚集 (正)
    signals["kurtosis"] = _clip((kurt - 1.0) / 3.0 * FACTOR_DIRECTION["kurtosis"])

    # reversion: 直接信号
    signals["reversion"] = _clip(reversion / 2.0 * FACTOR_DIRECTION["reversion"])

    # median_dist: 直接信号
    signals["median_dist"] = _clip(median_dist / 2.0 * FACTOR_DIRECTION["median_dist"])

    # reoccurrence: 重复占比 (0~1) 转信号; tsfresh显示IC~0.05, 占比>0.5才给正信号
    signals["reoccurrence"] = _clip((reoccurrence - 0.5) * 4.0 * FACTOR_DIRECTION["reoccurrence"])

    # 综合信号 (等权平均)
    composite = sum(signals.values()) / len(signals)

    # 状态判定
    if composite > 0.25:
        state = "bullish_factors"      # 因子偏多
    elif composite < -0.25:
        state = "bearish_factors"      # 因子偏空
    else:
        state = "neutral_factors"      # 因子中性

    return {
        "available": True,
        "factors": raw,
        "signals": signals,
        "composite": round(composite, 4),
        "state": state,
    }


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def format_factor_line(f: dict[str, object]) -> str:
    """生成可注入 prompt 的一行文本。"""
    if not f.get("available"):
        return "因子引擎: 数据不足, 不可用"
    sig = f["signals"]
    comp = f["composite"]
    state = f["state"]
    line = (
        f"统计因子引擎(基于9个月tick验证): 综合信号 {comp:+.2f} [{state}]; "
        f"动量反转 {sig['mom_6']:+.2f}, 收盘位置 {sig['close_pos']:+.2f}, "
        f"峰度聚集 {sig['kurtosis']:+.2f}, 回归压力 {sig['reversion']:+.2f}, "
        f"中位偏离 {sig['median_dist']:+.2f}, 重复价位 {sig['reoccurrence']:+.2f}"
    )
    return line
