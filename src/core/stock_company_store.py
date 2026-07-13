"""个股公司资料本地持久化：MongoDB 主存 + JSONL 降级。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.core.config import get_settings
from src.core.stock_market_utils import MARKET_HK, MARKET_US, is_overseas_market, market_from_a_code, resolve_stock

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
COMPANY_DIR = ROOT / "data" / "stock_company"

COLLECTION_PROFILE = "stock_company_profile"
COLLECTION_BALANCE = "stock_balance_sheet"


def _normalize_code(code: str, *, market: str | None = None) -> str:
    normalized, _ = resolve_stock(code, market)
    return normalized


def _doc_market(doc: dict[str, Any]) -> str:
    return (doc.get("market") or market_from_a_code(str(doc.get("code") or ""))).upper()


def _mongo_query(code: str, market: str) -> dict[str, Any]:
    if is_overseas_market(market):
        return {"code": code, "market": market}
    return {"code": code, "market": {"$in": [market, None, ""]}}


class StockCompanyStore:
    """个股详情 MongoDB / JSONL 读写。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        storage = (self._settings.stock_company_storage or "mongodb").strip().lower()
        self._storage_backends = {s.strip() for s in storage.split(",") if s.strip()}
        if not self._storage_backends:
            self._storage_backends = {"mongodb"}

    def _use_mongo(self) -> bool:
        return "mongodb" in self._storage_backends

    def _use_jsonl(self) -> bool:
        if "jsonl" in self._storage_backends:
            return True
        return self._use_mongo() and self._mongo_coll(COLLECTION_PROFILE) is None

    def _mongo_coll(self, name: str):
        uri = self._settings.mongodb_uri
        if not uri:
            return None
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            client.admin.command("ping")
            coll = client[self._settings.mongodb_db][name]
            if name == COLLECTION_PROFILE:
                coll.create_index([("code", 1), ("market", 1)], unique=True)
            elif name == COLLECTION_BALANCE:
                coll.create_index([("code", 1), ("report_date", 1)], unique=True)
                coll.create_index([("code", 1), ("report_date", -1)])
            return coll
        except Exception as exc:
            logger.warning("MongoDB %s 不可用: %s", name, exc)
            return None

    def _jsonl_path(self, code: str, *, market: str) -> Path:
        if market == MARKET_HK:
            return COMPANY_DIR / f"HK_{code}.json"
        if market == MARKET_US:
            return COMPANY_DIR / f"US_{code}.json"
        return COMPANY_DIR / f"{code}.json"

    def _parse_ts(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00").split("+")[0])
        except ValueError:
            return None

    def _is_fresh(self, doc: dict[str, Any], max_age_hours: float) -> bool:
        ts = self._parse_ts(doc.get("last_sync") or doc.get("updated_at") or doc.get("fetched_at"))
        if ts is None:
            return False
        return datetime.now() - ts < timedelta(hours=max_age_hours)

    def _strip_internal(self, doc: dict[str, Any]) -> dict[str, Any]:
        out = dict(doc)
        out.pop("_id", None)
        return out

    def save_company_detail(self, code: str, data: dict[str, Any], *, market: str | None = None) -> dict[str, Any]:
        """持久化个股详情；返回 storage / last_sync 元信息。"""
        code, market = resolve_stock(code, market or data.get("market"))
        if not code:
            return {"ok": False, "error": "code 必填"}

        now = datetime.now().isoformat()
        doc: dict[str, Any] = {
            "code": code,
            "market": market,
            "name": data.get("name") or "",
            "industry": data.get("industry") or "",
            "em_symbol": data.get("em_symbol") or "",
            "profile": data.get("profile") or {},
            "main_products": data.get("main_products") or [],
            "financial_summary": data.get("financial_summary") or {},
            "quote": data.get("quote") or {},
            "balance_sheet": data.get("balance_sheet") or {},
            "sources": data.get("sources") or {},
            "degraded": bool(data.get("degraded")),
            "degraded_parts": data.get("degraded_parts") or [],
            "disclaimer": data.get("disclaimer") or "",
            "fetched_at": data.get("fetched_at") or now,
            "updated_at": now,
            "last_sync": now,
        }

        storage_used: list[str] = []

        if self._use_mongo():
            coll = self._mongo_coll(COLLECTION_PROFILE)
            if coll is not None:
                try:
                    coll.update_one(
                        _mongo_query(code, market),
                        {"$set": doc, "$setOnInsert": {"created_at": now}},
                        upsert=True,
                    )
                    storage_used.append("mongodb")
                except Exception as exc:
                    logger.warning("MongoDB 公司简介写入失败 %s: %s", code, exc)

            raw_periods = data.get("_raw_balance_periods") or []
            if raw_periods:
                bs_coll = self._mongo_coll(COLLECTION_BALANCE)
                if bs_coll is not None:
                    for period in raw_periods:
                        report_date = period.get("report_date") or ""
                        if not report_date:
                            continue
                        bs_doc = {
                            "code": code,
                            "report_date": report_date,
                            "report_label": period.get("report_label") or "",
                            "items": period.get("items") or {},
                            "updated_at": now,
                        }
                        try:
                            bs_coll.update_one(
                                {"code": code, "report_date": report_date},
                                {"$set": bs_doc, "$setOnInsert": {"created_at": now}},
                                upsert=True,
                            )
                        except Exception as exc:
                            logger.debug("资产负债表 upsert 失败 %s %s: %s", code, report_date, exc)

        if self._use_jsonl() or not storage_used:
            try:
                path = self._jsonl_path(code, market=market)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
                if "jsonl" not in storage_used:
                    storage_used.append("jsonl")
            except Exception as exc:
                logger.warning("JSONL 公司简介写入失败 %s: %s", code, exc)

        storage = storage_used[0] if storage_used else "none"
        return {"ok": bool(storage_used), "code": code, "storage": storage, "last_sync": now}

    def load_company_detail(
        self,
        code: str,
        *,
        market: str | None = None,
        max_age_hours: Optional[float] = None,
        ignore_ttl: bool = False,
    ) -> Optional[dict[str, Any]]:
        """读取本地个股详情；过期或缺失返回 None（ignore_ttl=True 时忽略过期）。"""
        code, market = resolve_stock(code, market)
        if not code:
            return None

        if max_age_hours is None:
            max_age_hours = float(self._settings.stock_company_cache_hours)

        doc: Optional[dict[str, Any]] = None
        storage = "none"

        if self._use_mongo():
            coll = self._mongo_coll(COLLECTION_PROFILE)
            if coll is not None:
                try:
                    found = coll.find_one(_mongo_query(code, market))
                    if found:
                        doc = self._strip_internal(found)
                        storage = "mongodb"
                except Exception as exc:
                    logger.warning("MongoDB 公司简介读取失败 %s: %s", code, exc)

        if doc is None and self._use_jsonl():
            path = self._jsonl_path(code, market=market)
            if path.is_file():
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                    storage = "jsonl"
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("JSONL 读取失败 %s: %s", code, exc)

        if doc is None:
            return None
        if not ignore_ttl:
            if max_age_hours is None:
                max_age_hours = float(self._settings.stock_company_cache_hours)
            if not self._is_fresh(doc, max_age_hours):
                return None

        doc["storage"] = storage
        doc["market"] = doc.get("market") or market
        doc["last_sync"] = doc.get("last_sync") or doc.get("updated_at")
        doc["persisted"] = True
        doc["cached"] = False
        doc["ok"] = bool(doc.get("name") or doc.get("profile") or doc.get("main_products"))
        return doc

    def get_meta(self, code: str, *, market: str | None = None) -> dict[str, Any]:
        """返回存储元信息（不过期检查）。"""
        code, market = resolve_stock(code, market)
        if not code:
            return {"ok": False, "error": "code 必填"}

        if self._use_mongo():
            coll = self._mongo_coll(COLLECTION_PROFILE)
            if coll is not None:
                try:
                    doc = coll.find_one(_mongo_query(code, market))
                    if doc:
                        doc = self._strip_internal(doc)
                        return {
                            "ok": True,
                            "code": code,
                            "market": _doc_market(doc),
                            "name": doc.get("name") or "",
                            "storage": "mongodb",
                            "last_sync": doc.get("last_sync") or doc.get("updated_at"),
                            "fetched_at": doc.get("fetched_at"),
                            "degraded": doc.get("degraded"),
                        }
                except Exception as exc:
                    logger.warning("MongoDB meta 失败 %s: %s", code, exc)

        path = self._jsonl_path(code, market=market)
        if path.is_file():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                return {
                    "ok": True,
                    "code": code,
                    "market": _doc_market(doc),
                    "name": doc.get("name") or "",
                    "storage": "jsonl",
                    "last_sync": doc.get("last_sync") or doc.get("updated_at"),
                    "fetched_at": doc.get("fetched_at"),
                    "degraded": doc.get("degraded"),
                }
            except (json.JSONDecodeError, OSError):
                pass

        storage = "mongodb" if self._use_mongo() else ("jsonl" if self._use_jsonl() else "none")
        return {
            "ok": True,
            "code": code,
            "market": market,
            "name": "",
            "storage": storage,
            "last_sync": None,
            "fetched_at": None,
            "degraded": None,
        }


_store: Optional[StockCompanyStore] = None


def get_stock_company_store() -> StockCompanyStore:
    global _store
    if _store is None:
        _store = StockCompanyStore()
    return _store
