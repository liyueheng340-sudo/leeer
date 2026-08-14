"""Small helpers for MT5 server-time normalization in read-only sidecars."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def raw_mt5_utc_iso(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()


def infer_mt5_server_offset_seconds(
    *,
    capture_utc: datetime,
    raw_tick_time: int | None,
    tolerance_seconds: int = 300,
    round_seconds: int = 3600,
) -> int:
    if raw_tick_time is None:
        return 0
    raw_tick_utc = datetime.fromtimestamp(int(raw_tick_time), timezone.utc)
    delta_seconds = (raw_tick_utc - capture_utc.astimezone(timezone.utc)).total_seconds()
    if abs(delta_seconds) <= tolerance_seconds:
        return 0
    return int(round(delta_seconds / round_seconds) * round_seconds)


def corrected_mt5_utc_dt(timestamp: int | None, offset_seconds: int) -> datetime | None:
    if timestamp is None:
        return None
    raw_utc = datetime.fromtimestamp(int(timestamp), timezone.utc)
    return raw_utc - timedelta(seconds=int(offset_seconds))


def corrected_mt5_utc_iso(timestamp: int | None, offset_seconds: int) -> str | None:
    value = corrected_mt5_utc_dt(timestamp, offset_seconds)
    return value.isoformat() if value is not None else None


def session_label_from_corrected_utc(timestamp: int | None, offset_seconds: int) -> str | None:
    value = corrected_mt5_utc_dt(timestamp, offset_seconds)
    if value is None:
        return None
    hour = value.hour
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "london_ny_overlap"
    if 16 <= hour < 21:
        return "ny_late"
    if 21 <= hour < 22:
        return "rollover"
    return "off_hours"
