"""时区工具单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.core import timezone_utils as tz


def test_as_utc_naive_mongo_datetime():
    """PyMongo 读出的 naive UTC 在 CST 服务器上不得按本地时区解释。"""
    naive_utc = datetime(2026, 7, 10, 3, 31, 46)
    assert tz.as_utc(naive_utc).isoformat() == "2026-07-10T03:31:46+00:00"
    assert tz.format_display_dt(naive_utc) == "2026-07-10 11:31"


def test_format_display_dt_utc_to_shanghai():
    dt = datetime(2026, 6, 29, 16, 30, 0, tzinfo=timezone.utc)
    assert tz.format_display_dt(dt) == "2026-06-30 00:30"


def test_format_display_dt_naive_as_utc():
    dt = datetime(2026, 6, 29, 16, 30, 0)
    assert tz.format_display_dt(dt) == "2026-06-30 00:30"


def test_format_display_dt_iso_z():
    assert tz.format_display_dt("2026-06-29T16:30:00Z") == "2026-06-30 00:30"


def test_format_display_dt_missing():
    assert tz.format_display_dt(None) == "—"
    assert tz.format_display_dt("") == "—"


def test_coerce_dt_date_only():
    dt = tz.coerce_dt("2026-06-29")
    assert dt == datetime(2026, 6, 29, 0, 0, 0)


def test_to_app_tz_preserves_shanghai():
    sh = ZoneInfo("Asia/Shanghai")
    dt = datetime(2026, 6, 30, 8, 0, 0, tzinfo=sh)
    assert tz.to_app_tz(dt).strftime("%Y-%m-%d %H:%M") == "2026-06-30 08:00"


def test_data_service_format_dt(monkeypatch: pytest.MonkeyPatch):
    from src.web import data_service as ds

    monkeypatch.setattr(tz, "APP_TZ", ZoneInfo("Asia/Shanghai"))
    assert ds._format_dt(datetime(2026, 6, 29, 16, 0, 0, tzinfo=timezone.utc)) == "2026-06-30 00:00"


def test_compute_next_run_time_shanghai():
    from src.core.script_cron import compute_next_run_time

    after = datetime(2026, 6, 29, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    nxt = compute_next_run_time("0 2 * * *", after=after)
    assert nxt is not None
    assert nxt.tzinfo is not None
    assert nxt.hour == 2
    assert "+08:00" in nxt.isoformat() or nxt.utcoffset().total_seconds() == 8 * 3600
