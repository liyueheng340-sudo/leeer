"""Deterministic trading-session context for the XAU facts layer (A1).

交易时段上下文是纯时间函数，零模型成本、零网络：
- session_label：当前时段（asia / london / london_ny_overlap / ny_late）。
  与 guard.ACTIVE_SESSION_LABELS 同语义：london / london_ny_overlap / ny_late
  为活跃时段，asia 为非活跃时段（guard 会给出流动性标注）。
- session_name：时段中文名（供 prompt 与前端展示）。
- 距伦敦定盘（10:30 / 15:00 London）分钟数：黄金定盘是交易员共识的波动放大点。
- 距 COMEX 开盘（08:20 ET）分钟数：纽约期货开盘的流动性切换点。

时区用 zoneinfo + tzdata：伦敦（GMT/BST）与纽约（EST/EDT）的夏令时偏移由
IANA 时区库处理，不需要手工判断 DST。任何异常都返回 status="unavailable"
（失败安全，同 macro/news/iv 纪律），绝不阻断任务。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")

# 伦敦定盘时刻（London 本地时）：10:30 与 15:00。
LONDON_FIX_TIMES = ((10, 30), (15, 0))
# COMEX 黄金开盘时刻（ET 本地时）：08:20。
COMEX_OPEN_TIME = (8, 20)

# 时段划分（London 本地时分钟数）：活跃 = london / overlap / ny_late，
# 与 guard.ACTIVE_SESSION_LABELS 保持一致；其余为非活跃（流动性不足标注）。
_SESSION_BOUNDARIES: list[tuple[int, str, str]] = [
    (7 * 60, "london", "伦敦早盘"),
    (13 * 60 + 30, "london_ny_overlap", "伦敦-纽约重叠"),
    (16 * 60 + 30, "ny_late", "纽约午盘/尾盘"),
    (21 * 60, "asia", "隔夜/亚洲时段"),
]

# 注入 facts 的顶层键（prompt_rules 与前端消费）。
SESSION_LABEL_KEY = "session_label"
SESSION_CONTEXT_KEY = "session_context"


def _session_at(now: datetime) -> tuple[str, str]:
    """按 London 本地时间返回 (label, name)。

    入参任意 aware datetime，内部统一转换为 London 本地时（含夏令时）。
    区间（London 本地分钟数）：0-420 asia；420-810 london；810-990 overlap；
    990-1260 ny_late；1260-1440 asia。边界语义 = 区间起点，minutes >= 起点即落入。
    """
    now_london = now.astimezone(LONDON)
    minutes = now_london.hour * 60 + now_london.minute
    label = "asia"
    name = "隔夜/亚洲时段"
    for boundary, cand_label, cand_name in _SESSION_BOUNDARIES:
        if minutes < boundary:
            break
        label, name = cand_label, cand_name
    return label, name


def _minutes_to_next(
    now_local: datetime, tz: ZoneInfo, target_hour: int, target_minute: int
) -> int:
    """距下一个 target_hour:target_minute（本地时）的分钟数；含跨天。"""
    candidate = now_local.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return int((candidate - now_local).total_seconds() // 60)


def compute_session_context(now: datetime | None = None) -> dict[str, Any]:
    """返回交易时段上下文；never raises。

    status="ok" 时字段：
        label             asia / london / london_ny_overlap / ny_late
        name              时段中文名
        minutes_to_london_fix    距下一次伦敦定盘的分钟数（10:30/15:00 London）
        london_fix_at            该次定盘的 ISO 时刻（London 本地）
        minutes_to_comex_open    距下一次 COMEX 开盘的分钟数（08:20 ET）
        comex_open_at            该次开盘的 ISO 时刻（ET 本地）
        checked_at               UTC 检查时刻
    """
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        now_london = reference.astimezone(LONDON)
        now_ny = reference.astimezone(NEW_YORK)
        label, name = _session_at(now_london)
        # 最近一次伦敦定盘：10:30 / 15:00 中距离当前最近的下一个。
        candidates = [
            (h, mi, _minutes_to_next(now_london, LONDON, h, mi))
            for (h, mi) in LONDON_FIX_TIMES
        ]
        _fix_h, _fix_mi, _fix_minutes = min(candidates, key=lambda item: item[2])
        nearest_fix = now_london.replace(
            hour=_fix_h, minute=_fix_mi, second=0, microsecond=0
        )
        if nearest_fix <= now_london:
            nearest_fix += timedelta(days=1)
        comex_minutes = _minutes_to_next(now_ny, NEW_YORK, *COMEX_OPEN_TIME)
        comex_at = now_ny.replace(
            hour=COMEX_OPEN_TIME[0], minute=COMEX_OPEN_TIME[1], second=0, microsecond=0
        )
        if comex_at <= now_ny:
            comex_at += timedelta(days=1)
        return {
            "status": "ok",
            "label": label,
            "name": name,
            "minutes_to_london_fix": int(
                (nearest_fix - now_london).total_seconds() // 60
            ),
            "london_fix_at": nearest_fix.isoformat(),
            "minutes_to_comex_open": comex_minutes,
            "comex_open_at": comex_at.isoformat(),
            "checked_at": reference.isoformat(),
        }
    except Exception:
        return {"status": "unavailable", "reason": "交易时段计算异常"}
