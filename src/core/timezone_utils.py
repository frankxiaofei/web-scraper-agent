"""应用统一时区（默认 Asia/Shanghai，UTC+8）。"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

_DEFAULT_TZ = "Asia/Shanghai"


def get_app_tz() -> ZoneInfo:
    name = (os.environ.get("TZ") or _DEFAULT_TZ).strip() or _DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(_DEFAULT_TZ)


APP_TZ = get_app_tz()


def as_utc(dt: datetime) -> datetime:
    """MongoDB / PyMongo 返回的 naive datetime 按 UTC 处理（勿用 astimezone 默认本地时区）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_app_tz(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(APP_TZ)


def app_now() -> datetime:
    return datetime.now(APP_TZ)


def app_today() -> date:
    return app_now().date()


def coerce_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return None


def format_display_dt(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    dt = coerce_dt(value)
    if dt is None:
        return "—"
    return to_app_tz(dt).strftime(fmt)
