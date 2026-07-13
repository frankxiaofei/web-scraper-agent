"""InfluxDB 2.x 客户端：个股 K 线时序读写。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from src.core.config import get_settings

logger = logging.getLogger(__name__)

MEASUREMENT = "stock_kline"


def _parse_date_ts(date_str: str) -> datetime:
    """YYYY-MM-DD -> UTC 午夜 timestamp。"""
    d = date_str.replace("-", "")[:8]
    if len(d) == 8:
        return datetime(
            int(d[:4]), int(d[4:6]), int(d[6:8]),
            tzinfo=timezone.utc,
        )
    return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)


def _ts_to_date(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")


class InfluxKlineClient:
    """InfluxDB K 线读写；未配置或连接失败时 is_available() 为 False。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: Any = None
        self._write_api: Any = None
        self._query_api: Any = None
        self._available: Optional[bool] = None

    def _configured(self) -> bool:
        s = self._settings
        return bool(s.influxdb_url and s.influxdb_token and s.influxdb_org and s.influxdb_bucket)

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        if not self._configured():
            self._available = False
            return False
        try:
            self._ensure_client()
            self._available = self._client.ping()
        except Exception as exc:
            logger.warning("InfluxDB 不可用: %s", exc)
            self._available = False
        return self._available

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        s = self._settings
        self._client = InfluxDBClient(
            url=s.influxdb_url,
            token=s.influxdb_token,
            org=s.influxdb_org,
        )
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        self._query_api = self._client.query_api()

    def write_kline_points(self, rows: list[dict[str, Any]]) -> int:
        """写入 K 线点；同 code/period/date 会覆盖。"""
        if not rows or not self.is_available():
            return 0
        from influxdb_client import Point

        points = []
        for row in rows:
            date_str = row.get("date")
            if not date_str:
                continue
            market = str(row.get("market") or "SZ")
            p = (
                Point(MEASUREMENT)
                .tag("code", str(row.get("code", "")))
                .tag("period", str(row.get("period", "daily")))
                .tag("market", market)
                .time(_parse_date_ts(str(date_str)))
            )
            for field in (
                "open", "high", "low", "close", "volume", "amount",
                "change_pct", "change_amount", "turnover_rate", "amplitude",
            ):
                val = row.get(field)
                if val is not None:
                    p = p.field(field, float(val))
            name = row.get("name")
            if name:
                p = p.field("name", str(name))
            points.append(p)
        if not points:
            return 0
        try:
            self._ensure_client()
            self._write_api.write(
                bucket=self._settings.influxdb_bucket,
                org=self._settings.influxdb_org,
                record=points,
            )
            return len(points)
        except Exception as exc:
            logger.warning("InfluxDB K线写入失败: %s", exc)
            self._available = False
            return 0

    def query_kline(
        self,
        code: str,
        *,
        period: str = "daily",
        limit: int = 120,
        market: str | None = None,
    ) -> list[dict[str, Any]]:
        """按 code/period/limit 查询 OHLCV；market 用于区分 HK/US。"""
        if not self.is_available():
            return []
        limit = max(1, min(limit, 2000))
        market_filter = ""
        if market:
            market_filter = f'\n  |> filter(fn: (r) => r.market == "{market}")'
        flux = f'''
from(bucket: "{self._settings.influxdb_bucket}")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> filter(fn: (r) => r.code == "{code}")
  |> filter(fn: (r) => r.period == "{period}"){market_filter}
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: {limit})
  |> sort(columns: ["_time"])
'''
        try:
            self._ensure_client()
            tables = self._query_api.query(flux, org=self._settings.influxdb_org)
        except Exception as exc:
            logger.warning("InfluxDB K线查询失败: %s", exc)
            return []

        rows: list[dict[str, Any]] = []
        for table in tables:
            for record in table.records:
                ts = record.get_time()
                date_str = _ts_to_date(ts) if ts else ""
                name_val = record.values.get("name")
                market_val = record.values.get("market") or market or "SZ"
                rows.append({
                    "code": code,
                    "market": market_val,
                    "name": str(name_val) if name_val else "",
                    "period": period,
                    "date": date_str,
                    "open": _float_or_none(record.values.get("open")),
                    "high": _float_or_none(record.values.get("high")),
                    "low": _float_or_none(record.values.get("low")),
                    "close": _float_or_none(record.values.get("close")),
                    "volume": _float_or_none(record.values.get("volume")),
                    "amount": _float_or_none(record.values.get("amount")),
                    "change_pct": _float_or_none(record.values.get("change_pct")),
                    "change_amount": _float_or_none(record.values.get("change_amount")),
                    "turnover_rate": _float_or_none(record.values.get("turnover_rate")),
                    "amplitude": _float_or_none(record.values.get("amplitude")),
                })
        return rows

    def query_kline_meta(
        self, code: str, *, period: str = "daily", market: str | None = None
    ) -> Optional[dict[str, Any]]:
        """K 线元信息：条数、日期范围。"""
        if not self.is_available():
            return None
        market_filter = ""
        if market:
            market_filter = f'\n  |> filter(fn: (r) => r.market == "{market}")'
        flux = f'''
from(bucket: "{self._settings.influxdb_bucket}")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> filter(fn: (r) => r.code == "{code}")
  |> filter(fn: (r) => r.period == "{period}"){market_filter}
  |> filter(fn: (r) => r._field == "close")
  |> group()
  |> count()
'''
        flux_first = f'''
from(bucket: "{self._settings.influxdb_bucket}")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> filter(fn: (r) => r.code == "{code}")
  |> filter(fn: (r) => r.period == "{period}"){market_filter}
  |> filter(fn: (r) => r._field == "close")
  |> sort(columns: ["_time"])
  |> limit(n: 1)
'''
        flux_last = f'''
from(bucket: "{self._settings.influxdb_bucket}")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> filter(fn: (r) => r.code == "{code}")
  |> filter(fn: (r) => r.period == "{period}"){market_filter}
  |> filter(fn: (r) => r._field == "close")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 1)
'''
        flux_name = f'''
from(bucket: "{self._settings.influxdb_bucket}")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> filter(fn: (r) => r.code == "{code}")
  |> filter(fn: (r) => r.period == "{period}"){market_filter}
  |> filter(fn: (r) => r._field == "name")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 1)
'''
        try:
            self._ensure_client()
            count = 0
            for table in self._query_api.query(flux, org=self._settings.influxdb_org):
                for record in table.records:
                    count = int(record.get_value() or 0)
            if not count:
                return None

            first_date = last_date = None
            for table in self._query_api.query(flux_first, org=self._settings.influxdb_org):
                for record in table.records:
                    ts = record.get_time()
                    if ts:
                        first_date = _ts_to_date(ts)
            for table in self._query_api.query(flux_last, org=self._settings.influxdb_org):
                for record in table.records:
                    ts = record.get_time()
                    if ts:
                        last_date = _ts_to_date(ts)

            name = ""
            for table in self._query_api.query(flux_name, org=self._settings.influxdb_org):
                for record in table.records:
                    val = record.get_value()
                    if val:
                        name = str(val)

            return {
                "ok": True,
                "code": code,
                "market": market,
                "name": name,
                "period": period,
                "count": count,
                "start_date": first_date,
                "end_date": last_date,
                "last_updated": None,
                "source": "influxdb",
                "storage": "influxdb",
            }
        except Exception as exc:
            logger.warning("InfluxDB K线 meta 失败: %s", exc)
            return None


def _float_or_none(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


_client: Optional[InfluxKlineClient] = None


def get_influx_kline_client() -> InfluxKlineClient:
    global _client
    if _client is None:
        _client = InfluxKlineClient()
    return _client
