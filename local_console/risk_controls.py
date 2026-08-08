"""Dynamic risk controls that gate direction plans on recent measured outcomes.

方案 B（代码强制风控）动态部分（2026-08-07）：
- 连亏熔断（consecutive loss circuit breaker）：连续 N 单已判定为 SL_FIRST 后，
  禁止继续给出方向计划（directional_plan_allowed=False），但分析照常（军师模式
  不锁分析，只锁"继续真金白银承担风险"——这是宪法第九条"入场纪律层"的度量驱动扩展）。

设计原则（与 guard.py 一致）：否决权来自测量，不来自意见。熔断只消费已落盘的
复盘结果（outcome），不依赖任何预测；只禁方向建议，不新增 BLOCKED 路径。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .guard import GateResult

# 连亏熔断阈值：连续 N 单 SL_FIRST 后禁方向。默认 4（保守配置，2026-08-07 决策）：
# 探索显示既有 141 单上 3 连亏触发 10 次、跳过 39% 的单——过敏感会误伤正常交易。
# 4 连亏在 40% 胜率下更接近"持续放血"而非"正常波动"，熔断价值在拦住极端期。
LOSS_STREAK_THRESHOLD = 4
# 熔断回调：连续 N 单非亏损（TP_FIRST 或未触发）后解除。默认 2（保守）：
# 1 单 TP 恢复易反复触发（熔断-解除-又熔断），2 单非亏损让熔断更稳、
# 避免在糟糕期结束后立即重新开仓又被套。用"连续非亏损计数"作回调信号。
LOSS_STREAK_RECOVER_NON_LOSS = 2


def compute_loss_streak(records: list[Any]) -> int:
    """返回最近已判定记录的连续 SL_FIRST 数（按创建时间升序从最新往回数）。

    只统计已判定（TP_FIRST/SL_FIRST）样本；NOT_TRIGGERED/EXPIRED/PENDING 不参与
    （无 TP/SL 结果，不作为"连亏"也不作为"恢复"）。返回 0 表示无连亏。
    仅作诊断展示（熔断实际是否生效由 circuit_tripped 判定）。
    """
    decided: list[bool] = []
    for record in records:
        review = record.review
        if not isinstance(review, dict):
            continue
        outcome = review.get("outcome")
        if outcome == "SL_FIRST":
            decided.append(True)
        elif outcome == "TP_FIRST":
            decided.append(False)
        # 其他 outcome 跳过（不参与熔断判定）
    streak = 0
    for is_loss in reversed(decided):
        if is_loss:
            streak += 1
        else:
            break
    return streak


def _outcome_sequence(records: list[Any]) -> list[str]:
    """按时间升序提取已判定样本的 outcome（SL_FIRST/TP_FIRST/OTHER）。"""
    sequence: list[str] = []
    for record in records:
        review = record.review
        if not isinstance(review, dict):
            continue
        outcome = review.get("outcome")
        if outcome == "SL_FIRST":
            sequence.append("SL")
        elif outcome == "TP_FIRST":
            sequence.append("TP")
        else:
            sequence.append("OTHER")  # NOT_TRIGGERED/EXPIRED/PENDING：非亏损，可作恢复信号
    return sequence


def circuit_tripped(
    records: list[Any],
    threshold: int = LOSS_STREAK_THRESHOLD,
    recover: int = LOSS_STREAK_RECOVER_NON_LOSS,
) -> int | None:
    """连亏熔断状态机的当前"停机"判定：返回熔断时的连续亏损数；未熔断返回 None。

    状态机（2026-08-07 保守配置），按时间正序推进（老的→新的）：
    - 触发：连续 SL 达 threshold 次 → 熔断（记录该连续亏损数）。
    - 熔断中：任何 SL 保持熔断；连续 recover 次非亏损（TP/未触发/过期）→ 解除。
    - 未熔断：任一非亏损中断连亏计数。
    返回 None = 未熔断（可出方向）；返回 int = 已熔断（禁方向，值为熔断时连亏数）。
    """
    loss_run = 0
    tripped = False
    tripped_streak: int | None = None
    recover_count = 0
    for outcome in _outcome_sequence(records):
        if outcome == "SL":
            if tripped:
                # 熔断中遇 SL：保持熔断，恢复计数清零
                recover_count = 0
            else:
                loss_run += 1
                if loss_run >= threshold:
                    tripped = True
                    tripped_streak = loss_run
        else:  # TP 或 OTHER（非亏损）
            if tripped:
                recover_count += 1
                if recover_count >= recover:
                    tripped = False
                    tripped_streak = None
                    loss_run = 0
                    recover_count = 0
            else:
                loss_run = 0  # 未熔断时，非亏损中断连亏
    return tripped_streak if tripped else None


def circuit_breaker_reason(streak: int, threshold: int = LOSS_STREAK_THRESHOLD) -> str | None:
    """连亏达到阈值时返回禁方向原因；否则 None。"""
    if streak >= threshold:
        return f"连续 {streak} 单止损（连亏熔断，阈值 {threshold} 单），禁方向建议，仅观察"
    return None


def apply_circuit_breaker(
    gate: GateResult, streak: int, threshold: int = LOSS_STREAK_THRESHOLD
) -> GateResult:
    """连亏熔断：把 gate 的方向权限降级（禁方向），但保留分析（allow_model 不变）。

    用 dataclasses.replace 构造新 GateResult（frozen 不可变），追加熔断标注。
    不新增 BLOCKED（allow_model 恒 True），符合宪法"军师模式不锁分析"。
    """
    reason = circuit_breaker_reason(streak, threshold)
    if reason is None:
        return gate
    new_warnings = tuple(w for w in gate.warnings) + (reason,)
    return replace(
        gate,
        directional_plan_allowed=False,
        reason=f"{gate.reason}；连亏熔断（连续 {streak} 单止损）",
        warnings=new_warnings,
    )
