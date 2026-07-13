"""个股 K 线历史行情：AKShare 拉取 + MongoDB/JSONL 持久化。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.core.config import get_settings
from src.core.influx_client import get_influx_kline_client
from src.core.stock_market_data import _akshare_call, _akshare_import, is_akshare_available
from src.core.stock_market_utils import MARKET_HK, MARKET_US, is_overseas_market, resolve_stock, resolve_us_secid

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
KLINE_DIR = ROOT / "data" / "stock_kline"

COLLECTION = "stock_kline_daily"
VALID_PERIODS = frozenset({"daily", "weekly", "monthly"})
PERIOD_LABELS = {"daily": "日K", "weekly": "周K", "monthly": "月K"}


def _normalize_code(code: str, *, market: str | None = None) -> str:
    normalized, _ = resolve_stock(code, market)
    return normalized


def _storage_key(code: str, market: str) -> str:
    if is_overseas_market(market):
        return f"{market}:{code}"
    return code


def _tx_symbol(code: str) -> str:
    """AKShare 腾讯接口市场前缀。"""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def _date_for_tx(d: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD"""
    d = d.replace("-", "")
    if len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _default_start_date(days: int = 365) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


def _default_end_date() -> str:
    return datetime.now().strftime("%Y%m%d")


def _parse_row(row: Any, *, code: str, name: str, period: str, market: str) -> dict[str, Any]:
    date_val = row.get("日期")
    if hasattr(date_val, "isoformat"):
        date_str = date_val.isoformat()
    else:
        date_str = str(date_val)[:10]

    def _num(key: str) -> Optional[float]:
        val = row.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    return {
        "code": code,
        "market": market,
        "name": name,
        "period": period,
        "date": date_str,
        "open": _num("开盘"),
        "high": _num("最高"),
        "low": _num("最低"),
        "close": _num("收盘"),
        "volume": _num("成交量"),
        "amount": _num("成交额"),
        "change_pct": _num("涨跌幅"),
        "change_amount": _num("涨跌额"),
        "turnover_rate": _num("换手率"),
        "amplitude": _num("振幅"),
    }


class StockKlineService:
    """个股 K 线拉取、存储与查询。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._storage_backends = {
            s.strip().lower()
            for s in (self._settings.kline_storage or "").split(",")
            if s.strip()
        }

    def _storage_has(self, name: str) -> bool:
        return name in self._storage_backends

    def _use_influx(self) -> bool:
        return self._storage_has("influxdb")

    def _influx_ready(self) -> bool:
        return self._use_influx() and get_influx_kline_client().is_available()

    def _use_mongo(self) -> bool:
        if self._storage_has("mongodb"):
            return True
        if self._use_influx():
            return not get_influx_kline_client().is_available()
        return not self._storage_backends

    def _use_jsonl(self) -> bool:
        if self._storage_has("jsonl"):
            return True
        if self._use_influx():
            return not get_influx_kline_client().is_available()
        return not self._storage_backends

    def _mongo_coll(self):
        uri = self._settings.mongodb_uri
        if not uri:
            return None
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            client.admin.command("ping")
            coll = client[self._settings.mongodb_db][COLLECTION]
            coll.create_index([("code", 1), ("period", 1), ("date", 1)], unique=True)
            coll.create_index([("code", 1), ("period", 1), ("date", -1)])
            return coll
        except Exception as exc:
            logger.warning("MongoDB %s 不可用: %s", COLLECTION, exc)
            return None

    def _jsonl_path(self, code: str, period: str, *, market: str) -> Path:
        key = _storage_key(code, market)
        return KLINE_DIR / period / f"{key}.jsonl"

    def _save_jsonl(self, code: str, period: str, rows: list[dict[str, Any]], *, market: str) -> None:
        if not rows:
            return
        path = self._jsonl_path(code, period, market=market)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, dict[str, Any]] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    existing[item["date"]] = item
                except (json.JSONDecodeError, KeyError):
                    continue
        for row in rows:
            existing[row["date"]] = row
        sorted_rows = sorted(existing.values(), key=lambda r: r["date"])
        with path.open("w", encoding="utf-8") as f:
            for row in sorted_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _load_jsonl(self, code: str, period: str, *, limit: int, market: str) -> list[dict[str, Any]]:
        path = self._jsonl_path(code, period, market=market)
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        rows.sort(key=lambda r: r.get("date") or "")
        if limit and len(rows) > limit:
            rows = rows[-limit:]
        return rows

    def _mongo_filter(self, code: str, period: str, market: str) -> dict[str, Any]:
        filt: dict[str, Any] = {"code": code, "period": period}
        if is_overseas_market(market):
            filt["market"] = market
        return filt

    def _lookup_us_secid(self, ticker: str) -> str:
        secid = resolve_us_secid(ticker)
        if "." in secid and secid.split(".")[0].isdigit():
            return secid
        ak = _akshare_import()
        if ak is None:
            return secid
        try:
            df = _akshare_call(lambda: ak.stock_us_spot_em(), label="美股secid查询")
            target = ticker.upper()
            for _, row in df.iterrows():
                em_code = str(row.get("代码", "")).strip()
                short = str(row.get("简称", "")).strip().upper()
                if short == target or em_code.endswith(f".{target}"):
                    from src.core.stock_market_utils import register_us_secid

                    register_us_secid(target, em_code)
                    return em_code
        except Exception as exc:
            logger.debug("美股 secid 查询失败 %s: %s", ticker, exc)
        return secid

    def _upsert_rows(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = datetime.now().isoformat()
        saved = 0

        if self._influx_ready():
            saved = get_influx_kline_client().write_kline_points(rows)

        if self._use_mongo():
            coll = self._mongo_coll()
            if coll is not None:
                for row in rows:
                    doc = {**row, "updated_at": now}
                    filt = self._mongo_filter(
                        row["code"],
                        row["period"],
                        row.get("market") or "SZ",
                    )
                    try:
                        coll.update_one(
                            {**filt, "date": row["date"]},
                            {"$set": doc, "$setOnInsert": {"created_at": now}},
                            upsert=True,
                        )
                        saved = max(saved, 1)
                    except Exception as exc:
                        logger.debug("K线 upsert 失败 %s: %s", row.get("date"), exc)

        if self._use_jsonl():
            code = rows[0]["code"]
            period = rows[0]["period"]
            market = rows[0].get("market") or "SZ"
            self._save_jsonl(code, period, rows, market=market)
            saved = saved or len(rows)

        return saved or len(rows)

    def _fetch_from_akshare(
        self,
        code: str,
        *,
        market: str,
        period: str,
        start: str,
        end: str,
    ) -> Any:
        ak = _akshare_import()
        assert ak is not None
        if market == MARKET_HK:
            return _akshare_call(
                lambda: ak.stock_hk_hist(
                    symbol=code,
                    period=period,
                    start_date=start,
                    end_date=end,
                    adjust="",
                ),
                label=f"港股K线({code},{period})",
            )
        if market == MARKET_US:
            secid = self._lookup_us_secid(code)
            return _akshare_call(
                lambda: ak.stock_us_hist(
                    symbol=secid,
                    period=period,
                    start_date=start,
                    end_date=end,
                    adjust="",
                ),
                label=f"美股K线({code},{period})",
            )
        try:
            return _akshare_call(
                lambda: ak.stock_zh_a_hist(
                    symbol=code,
                    period=period,
                    start_date=start,
                    end_date=end,
                    adjust="",
                ),
                label=f"K线({code},{period})",
            )
        except Exception as exc:
            if period != "daily":
                raise
            logger.warning("东方财富 K线失败 %s，尝试腾讯备用: %s", code, exc)
            tx_sym = _tx_symbol(code)
            return _akshare_call(
                lambda: ak.stock_zh_a_hist_tx(
                    symbol=tx_sym,
                    start_date=_date_for_tx(start),
                    end_date=_date_for_tx(end),
                    adjust="",
                ),
                label=f"K线腾讯({code})",
            )

    def _rows_from_df(self, df: Any, *, code: str, name: str, period: str, market: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cols = set(df.columns)
        for _, row in df.iterrows():
            if "日期" in cols:
                rows.append(_parse_row(row, code=code, name=name, period=period, market=market))
            else:
                date_val = row.get("date")
                if hasattr(date_val, "isoformat"):
                    date_str = date_val.isoformat()
                else:
                    date_str = str(date_val)[:10]
                def _num(key: str) -> Optional[float]:
                    val = row.get(key)
                    if val is None:
                        return None
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        return None
                rows.append({
                    "code": code,
                    "market": market,
                    "name": name,
                    "period": period,
                    "date": date_str,
                    "open": _num("open"),
                    "high": _num("high"),
                    "low": _num("low"),
                    "close": _num("close"),
                    "volume": _num("volume"),
                    "amount": _num("amount"),
                    "change_pct": _num("change_pct"),
                    "change_amount": None,
                    "turnover_rate": None,
                    "amplitude": None,
                })
        return rows

    def _fetch_stock_name(self, code: str, *, market: str) -> str:
        if market == MARKET_HK:
            ak = _akshare_import()
            if ak is None:
                return ""
            try:
                df = _akshare_call(
                    lambda: ak.stock_hk_security_profile_em(symbol=code),
                    label=f"港股名称({code})",
                )
                if df is not None and not df.empty:
                    return str(df.iloc[0].get("证券简称") or "").strip()
            except Exception as exc:
                logger.debug("获取港股名称失败 %s: %s", code, exc)
            return ""

        if market == MARKET_US:
            ak = _akshare_import()
            if ak is None:
                return ""
            try:
                df = _akshare_call(lambda: ak.stock_us_spot_em(), label="美股名称查询")
                target = code.upper()
                for _, row in df.iterrows():
                    short = str(row.get("简称", "")).strip().upper()
                    if short == target:
                        return str(row.get("名称", short)).strip()
            except Exception as exc:
                logger.debug("获取美股名称失败 %s: %s", code, exc)
            return code

        ak = _akshare_import()
        if ak is None:
            return ""
        try:
            df = _akshare_call(
                lambda: ak.stock_individual_info_em(symbol=code),
                label=f"个股名称({code})",
            )
            for _, row in df.iterrows():
                key = str(row.get("item") or "")
                if key in ("股票简称", "名称"):
                    return str(row.get("value") or "").strip()
        except Exception as exc:
            logger.debug("获取个股名称失败 %s: %s", code, exc)
        return ""

    def fetch_and_save_kline(
        self,
        code: str,
        *,
        market: str | None = None,
        period: str = "daily",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        """从 AKShare 拉取 K 线并 upsert 到本地。"""
        code, market = resolve_stock(code, market)
        period = (period or "daily").strip().lower()
        if period not in VALID_PERIODS:
            return {"ok": False, "error": f"不支持的 period: {period}"}
        if not is_akshare_available():
            return {"ok": False, "error": "akshare 未安装或未启用", "degraded": True}

        start = (start_date or _default_start_date()).replace("-", "")
        end = (end_date or _default_end_date()).replace("-", "")
        stock_name = (name or "").strip() or self._fetch_stock_name(code, market=market)

        try:
            df = self._fetch_from_akshare(code, market=market, period=period, start=start, end=end)
        except Exception as exc:
            logger.warning("AKShare K线拉取失败 %s (%s): %s", code, market, exc)
            return {
                "ok": False,
                "code": code,
                "market": market,
                "period": period,
                "error": str(exc),
                "degraded": True,
            }

        if df is None or df.empty:
            return {
                "ok": False,
                "code": code,
                "market": market,
                "period": period,
                "error": "无 K 线数据",
                "count": 0,
            }

        rows = self._rows_from_df(df, code=code, name=stock_name, period=period, market=market)
        count = self._upsert_rows(rows)
        return {
            "ok": True,
            "code": code,
            "market": market,
            "name": stock_name,
            "period": period,
            "count": count,
            "start_date": rows[0]["date"] if rows else None,
            "end_date": rows[-1]["date"] if rows else None,
            "fetched_at": datetime.now().isoformat(),
        }

    def get_kline_history(
        self,
        code: str,
        *,
        market: str | None = None,
        period: str = "daily",
        limit: int = 120,
    ) -> list[dict[str, Any]]:
        """读取本地 K 线（InfluxDB 优先，MongoDB/JSONL 降级）。"""
        code, market = resolve_stock(code, market)
        period = (period or "daily").strip().lower()
        limit = max(1, min(limit, 2000))

        if self._influx_ready():
            rows = get_influx_kline_client().query_kline(
                code, period=period, limit=limit, market=market
            )
            if rows:
                if is_overseas_market(market):
                    overseas_rows = [r for r in rows if (r.get("market") or market) == market]
                    if overseas_rows:
                        return overseas_rows
                elif not any(is_overseas_market(r.get("market")) for r in rows):
                    return rows

        if self._use_mongo():
            coll = self._mongo_coll()
            if coll is not None:
                try:
                    filt = self._mongo_filter(code, period, market)
                    docs = list(
                        coll.find(filt)
                        .sort("date", -1)
                        .limit(limit)
                    )
                    if docs:
                        for doc in docs:
                            doc.pop("_id", None)
                        docs.reverse()
                        return docs
                except Exception as exc:
                    logger.warning("MongoDB K线读取失败: %s", exc)

        if self._use_jsonl():
            return self._load_jsonl(code, period, limit=limit, market=market)
        return []

    def get_kline_meta(self, code: str, *, market: str | None = None, period: str = "daily") -> dict[str, Any]:
        """K 线元信息：条数、日期范围、最后更新。"""
        code, market = resolve_stock(code, market)
        period = (period or "daily").strip().lower()

        if self._influx_ready():
            meta = get_influx_kline_client().query_kline_meta(code, period=period, market=market)
            if meta:
                meta["market"] = market
                return meta

        if self._use_mongo():
            coll = self._mongo_coll()
            if coll is not None:
                try:
                    filt = self._mongo_filter(code, period, market)
                    total = coll.count_documents(filt)
                    if total:
                        first = coll.find_one(filt, sort=[("date", 1)])
                        last = coll.find_one(filt, sort=[("date", -1)])
                        name = (last or first or {}).get("name") or ""
                        return {
                            "ok": True,
                            "code": code,
                            "market": market,
                            "name": name,
                            "period": period,
                            "count": total,
                            "start_date": (first or {}).get("date"),
                            "end_date": (last or {}).get("date"),
                            "last_updated": (last or {}).get("updated_at"),
                            "source": "mongodb",
                            "storage": "mongodb",
                        }
                except Exception as exc:
                    logger.warning("MongoDB K线 meta 失败: %s", exc)

        if self._use_jsonl():
            rows = self._load_jsonl(code, period, limit=0, market=market)
            if rows:
                return {
                    "ok": True,
                    "code": code,
                    "market": market,
                    "name": rows[-1].get("name") or "",
                    "period": period,
                    "count": len(rows),
                    "start_date": rows[0].get("date"),
                    "end_date": rows[-1].get("date"),
                    "last_updated": rows[-1].get("updated_at"),
                    "source": "jsonl",
                    "storage": "jsonl",
                }
        storage = "influxdb" if "influxdb" in self._storage_backends else "none"
        return {
            "ok": True,
            "code": code,
            "market": market,
            "name": "",
            "period": period,
            "count": 0,
            "start_date": None,
            "end_date": None,
            "last_updated": None,
            "source": "none",
            "storage": storage,
        }

    def get_kline(
        self,
        code: str,
        *,
        market: str | None = None,
        period: str = "daily",
        limit: int = 120,
        auto_fetch: bool = True,
    ) -> dict[str, Any]:
        """优先读本地；无数据或不足时拉 AKShare 并保存。"""
        code, market = resolve_stock(code, market)
        period = (period or "daily").strip().lower()
        limit = max(1, min(limit, 500))
        rows = self.get_kline_history(code, market=market, period=period, limit=limit)
        fetched = False
        if auto_fetch and len(rows) < min(limit, 30):
            result = self.fetch_and_save_kline(code, market=market, period=period)
            if result.get("ok"):
                fetched = True
                rows = self.get_kline_history(code, market=market, period=period, limit=limit)
            elif not rows:
                return {
                    "ok": False,
                    "code": code,
                    "market": market,
                    "period": period,
                    "items": [],
                    "error": result.get("error"),
                    "degraded": result.get("degraded", False),
                }
        meta = self.get_kline_meta(code, market=market, period=period)
        return {
            "ok": bool(rows),
            "code": code,
            "market": market,
            "name": meta.get("name") or (rows[-1].get("name") if rows else ""),
            "period": period,
            "period_label": PERIOD_LABELS.get(period, period),
            "items": rows,
            "count": len(rows),
            "fetched": fetched,
            "meta": meta,
            "disclaimer": "行情数据仅供参考，不构成投资建议。",
        }


_service: Optional[StockKlineService] = None


def get_stock_kline_service() -> StockKlineService:
    global _service
    if _service is None:
        _service = StockKlineService()
    return _service
