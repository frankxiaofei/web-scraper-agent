"""A 股 / 港股个股检索：索引构建、本地缓存与模糊匹配。"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.core.config import get_settings
from src.core.stock_market_data import _akshare_call, _akshare_import, is_akshare_available
from src.core.stock_market_utils import (
    MARKET_HK,
    MARKET_US,
    is_hk_enabled,
    is_us_enabled,
    market_from_a_code,
    normalize_a_code,
    normalize_hk_code,
    normalize_us_symbol,
    register_us_secid,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_JSON_PATH = ROOT / "data" / "stock_search_index.json"
COLLECTION = "stock_search_index"
DOC_ID = "stock_list"
DOC_ID_LEGACY = "a_share_list"
DEFAULT_CACHE_HOURS = 24.0

_index_lock = threading.Lock()
_memory_items: list[dict[str, Any]] | None = None
_memory_meta: dict[str, Any] | None = None
_hk_lookup_cache: tuple[float, list[dict[str, Any]]] | None = None
_us_lookup_cache: tuple[float, list[dict[str, Any]]] | None = None
_HK_LOOKUP_TTL = 3600.0
_US_LOOKUP_TTL = 3600.0


def _normalize_code(code: str, *, market: str | None = None) -> str:
    m = (market or "").strip().upper()
    if m == MARKET_HK:
        return normalize_hk_code(code)
    return normalize_a_code(code)


def _market_from_code(code: str) -> str:
    return market_from_a_code(code)


def _pinyin_initials(name: str) -> str:
    try:
        from pypinyin import Style, lazy_pinyin

        return "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)).lower()
    except ImportError:
        return ""


def _parse_ts(value: Any) -> Optional[datetime]:
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


class StockSearchService:
    """构建/缓存 A 股 + 港股列表，支持代码、名称、拼音首字母模糊检索。"""

    def __init__(self) -> None:
        self._settings = get_settings()

    def _cache_hours(self) -> float:
        return max(0.0, float(self._settings.stock_search_cache_hours))

    def _hk_enabled(self) -> bool:
        return is_hk_enabled()

    def _us_enabled(self) -> bool:
        return is_us_enabled()

    def _mongo_coll(self):
        uri = self._settings.mongodb_uri
        if not uri:
            return None
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            client.admin.command("ping")
            return client[self._settings.mongodb_db][COLLECTION]
        except Exception as exc:
            logger.warning("MongoDB %s 不可用: %s", COLLECTION, exc)
            return None

    def _is_fresh(self, updated_at: Any, max_age_hours: float) -> bool:
        ts = _parse_ts(updated_at)
        if ts is None:
            return False
        return datetime.now() - ts < timedelta(hours=max_age_hours)

    def _fetch_a_share_from_akshare(self) -> list[dict[str, Any]]:
        if not is_akshare_available():
            raise RuntimeError("AKShare 未安装或不可用")
        ak = _akshare_import()
        assert ak is not None
        df = _akshare_call(lambda: ak.stock_info_a_code_name(), label="stock_info_a_code_name")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, row in df.iterrows():
            code = _normalize_code(str(row.get("code", "")))
            name = str(row.get("name", "")).strip()
            if not code or not name or code in seen:
                continue
            seen.add(code)
            items.append(
                {
                    "code": code,
                    "name": name,
                    "market": _market_from_code(code),
                    "initials": _pinyin_initials(name),
                }
            )
        items.sort(key=lambda x: x["code"])
        return items

    def _fetch_hk_from_akshare(self) -> list[dict[str, Any]]:
        if not is_akshare_available():
            raise RuntimeError("AKShare 未安装或不可用")
        ak = _akshare_import()
        assert ak is not None
        sources = [
            ("stock_hk_ggt_components_em", lambda: ak.stock_hk_ggt_components_em()),
            ("stock_hk_spot_em", lambda: ak.stock_hk_spot_em()),
        ]
        last_exc: Exception | None = None
        df = None
        source_label = ""
        for label, fn in sources:
            try:
                df = _akshare_call(fn, label=label)
                source_label = label
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("港股列表 %s 失败: %s", label, exc)
        if df is None:
            raise last_exc or RuntimeError("港股列表拉取失败")

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, row in df.iterrows():
            code = normalize_hk_code(str(row.get("代码", row.get("code", ""))))
            name = str(row.get("名称", row.get("name", ""))).strip()
            if not code or not name or code in seen:
                continue
            seen.add(code)
            items.append(
                {
                    "code": code,
                    "name": name,
                    "market": MARKET_HK,
                    "initials": _pinyin_initials(name),
                }
            )
        items.sort(key=lambda x: x["code"])
        logger.info("港股检索索引来源 %s，共 %d 条", source_label, len(items))
        return items

    def _parse_us_spot_df(self, df: Any, *, source_label: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, row in df.iterrows():
            em_code = str(row.get("代码", row.get("code", ""))).strip()
            ticker = ""
            if em_code and "." in em_code:
                ticker = em_code.split(".")[-1].upper()
            else:
                ticker = str(row.get("简称", row.get("name", ""))).strip().upper()
            name = str(row.get("名称", row.get("name", ""))).strip() or ticker
            if not ticker or not name or ticker in seen:
                continue
            seen.add(ticker)
            if em_code:
                register_us_secid(ticker, em_code)
            items.append(
                {
                    "code": ticker,
                    "name": name,
                    "market": MARKET_US,
                    "em_secid": em_code or None,
                    "initials": _pinyin_initials(name),
                }
            )
        logger.info("美股检索索引来源 %s，共 %d 条", source_label, len(items))
        return items

    def _fetch_us_from_akshare(self) -> list[dict[str, Any]]:
        if not is_akshare_available():
            raise RuntimeError("AKShare 未安装或不可用")
        ak = _akshare_import()
        assert ak is not None
        sources: list[tuple[str, Any]] = [
            ("stock_us_spot_em", lambda: ak.stock_us_spot_em()),
        ]
        for cat in ("科技类", "金融类", "医药食品类", "媒体类", "汽车能源类", "制造零售类"):
            sources.append(
                (f"stock_us_famous_spot_em({cat})", lambda c=cat: ak.stock_us_famous_spot_em(symbol=c))
            )
        merged: dict[str, dict[str, Any]] = {}
        last_exc: Exception | None = None
        for label, fn in sources:
            try:
                df = _akshare_call(fn, label=label)
                for item in self._parse_us_spot_df(df, source_label=label):
                    merged[item["code"]] = item
                if label == "stock_us_spot_em" and merged:
                    break
            except Exception as exc:
                last_exc = exc
                logger.warning("美股列表 %s 失败: %s", label, exc)
        if not merged:
            raise last_exc or RuntimeError("美股列表拉取失败")
        items = sorted(merged.values(), key=lambda x: x["code"])
        return items

    def _fetch_from_akshare(self, *, cached_items: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], str]:
        a_items = self._fetch_a_share_from_akshare()
        sources = ["akshare:stock_info_a_code_name"]
        merged = list(a_items)
        if self._hk_enabled():
            try:
                hk_items = self._fetch_hk_from_akshare()
                merged.extend(hk_items)
                sources.append("akshare:stock_hk")
            except Exception as exc:
                logger.warning("港股列表合并失败: %s", exc)
                old_hk = [
                    item for item in (cached_items or []) if item.get("market") == MARKET_HK
                ]
                if old_hk:
                    merged.extend(old_hk)
                    sources.append("cache:hk_degraded")
        if self._us_enabled():
            try:
                us_items = self._fetch_us_from_akshare()
                merged.extend(us_items)
                sources.append("akshare:stock_us")
            except Exception as exc:
                logger.warning("美股列表合并失败: %s", exc)
                old_us = [
                    item for item in (cached_items or []) if item.get("market") == MARKET_US
                ]
                if old_us:
                    merged.extend(old_us)
                    sources.append("cache:us_degraded")
        merged.sort(key=lambda x: (x.get("market") or "", x.get("code") or ""))
        return merged, "+".join(sources)

    def _persist_index(self, items: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
        now = datetime.now().isoformat()
        meta = {
            "_id": DOC_ID,
            "updated_at": now,
            "count": len(items),
            "source": source,
            "hk_enabled": self._hk_enabled(),
            "us_enabled": self._us_enabled(),
            "items": items,
        }
        coll = self._mongo_coll()
        if coll is not None:
            try:
                coll.replace_one({"_id": DOC_ID}, meta, upsert=True)
            except Exception as exc:
                logger.warning("写入 Mongo 检索索引失败: %s", exc)
        try:
            INDEX_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            INDEX_JSON_PATH.write_text(
                json.dumps({"updated_at": now, "count": len(items), "items": items}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("写入 JSON 检索索引失败: %s", exc)
        return {"updated_at": now, "count": len(items), "source": meta["source"]}

    def _load_cached_index(self) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        coll = self._mongo_coll()
        if coll is not None:
            for doc_id in (DOC_ID, DOC_ID_LEGACY):
                try:
                    doc = coll.find_one({"_id": doc_id})
                    if doc and doc.get("items"):
                        return list(doc["items"]), {
                            "updated_at": doc.get("updated_at"),
                            "count": doc.get("count"),
                            "source": doc.get("source"),
                            "storage": "mongodb",
                        }
                except Exception as exc:
                    logger.warning("读取 Mongo 检索索引失败: %s", exc)

        if INDEX_JSON_PATH.exists():
            try:
                payload = json.loads(INDEX_JSON_PATH.read_text(encoding="utf-8"))
                items = payload.get("items") or []
                if items:
                    return items, {
                        "updated_at": payload.get("updated_at"),
                        "count": payload.get("count", len(items)),
                        "source": "json",
                        "storage": "json",
                    }
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("读取 JSON 检索索引失败: %s", exc)
        return None, None

    def ensure_index(self, *, refresh: bool = False) -> dict[str, Any]:
        """构建或刷新检索索引。"""
        global _memory_items, _memory_meta
        cache_hours = self._cache_hours()

        with _index_lock:
            if not refresh and _memory_items is not None and _memory_meta:
                if self._is_fresh(_memory_meta.get("updated_at"), cache_hours):
                    return {"ok": True, "refreshed": False, **_memory_meta}

            cached_items, cached_meta = self._load_cached_index()
            if (
                not refresh
                and cached_items
                and cached_meta
                and self._is_fresh(cached_meta.get("updated_at"), cache_hours)
            ):
                _memory_items = cached_items
                _memory_meta = cached_meta
                return {"ok": True, "refreshed": False, **cached_meta}

            try:
                items, source = self._fetch_from_akshare(cached_items=cached_items)
                meta = self._persist_index(items, source=source)
                _memory_items = items
                _memory_meta = meta
                return {"ok": True, "refreshed": True, **meta}
            except Exception as exc:
                logger.warning("AKShare 构建检索索引失败: %s", exc)
                if cached_items:
                    _memory_items = cached_items
                    _memory_meta = {**(cached_meta or {}), "degraded": True, "error": str(exc)}
                    return {"ok": True, "refreshed": False, "degraded": True, **(_memory_meta or {})}
                return {"ok": False, "error": str(exc)}

    def _get_items(self, *, refresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        result = self.ensure_index(refresh=refresh)
        if not result.get("ok"):
            return [], result
        with _index_lock:
            return list(_memory_items or []), result

    @staticmethod
    def _match_score(item: dict[str, Any], query: str, q_lower: str) -> int:
        import re

        code = item.get("code") or ""
        name = item.get("name") or ""
        initials = (item.get("initials") or "").lower()
        market = (item.get("market") or "").upper()
        digits = re.sub(r"\D", "", query)
        if market == MARKET_HK:
            q_code = digits.zfill(5) if digits else query
            if digits and len(digits) <= 5 and code == q_code:
                return 1100
        elif market == MARKET_US:
            q_ticker = query.upper()
            if code == q_ticker:
                return 1100
            if code.startswith(q_ticker):
                return 950 - len(q_ticker)
        else:
            q_code = query.zfill(6) if query.isdigit() else query

        if code == q_code or code == query:
            return 1000
        if code.startswith(q_code) or code.startswith(query):
            return 950 - len(query)
        if query in code or q_code in code:
            return 850
        if name == query:
            return 800
        if query in name:
            return 750 - name.index(query)
        if q_lower and initials.startswith(q_lower):
            return 700 - len(q_lower)
        if q_lower and q_lower in initials:
            return 650
        if q_lower and q_lower in name.lower():
            return 600
        return 0

    def _lookup_hk_items(self) -> list[dict[str, Any]]:
        """按需拉取港股通列表，供检索索引缺失时的降级匹配。"""
        global _hk_lookup_cache
        if not self._hk_enabled():
            return []
        now = datetime.now().timestamp()
        if _hk_lookup_cache and now - _hk_lookup_cache[0] < _HK_LOOKUP_TTL:
            return list(_hk_lookup_cache[1])
        try:
            items = self._fetch_hk_from_akshare()
            _hk_lookup_cache = (now, items)
            return items
        except Exception as exc:
            logger.warning("港股直通检索列表失败: %s", exc)
            return _hk_lookup_cache[1] if _hk_lookup_cache else []

    def _lookup_us_items(self) -> list[dict[str, Any]]:
        """按需拉取美股列表，供检索索引缺失时的降级匹配。"""
        global _us_lookup_cache
        if not self._us_enabled():
            return []
        now = datetime.now().timestamp()
        if _us_lookup_cache and now - _us_lookup_cache[0] < _US_LOOKUP_TTL:
            return list(_us_lookup_cache[1])
        try:
            items = self._fetch_us_from_akshare()
            _us_lookup_cache = (now, items)
            return items
        except Exception as exc:
            logger.warning("美股直通检索列表失败: %s", exc)
            return _us_lookup_cache[1] if _us_lookup_cache else []

    def _lookup_us_by_code(self, query: str) -> list[dict[str, Any]]:
        from src.core.stock_market_utils import is_us_ticker

        if not is_us_ticker(query) or not is_akshare_available():
            return []
        ticker = normalize_us_symbol(query)
        pool = self._lookup_us_items()
        for item in pool:
            if item.get("code") == ticker:
                return [item]
        return [{"code": ticker, "name": ticker, "market": MARKET_US, "initials": ""}]

    def _lookup_hk_by_code(self, query: str) -> list[dict[str, Any]]:
        import re

        digits = re.sub(r"\D", "", query)
        if not digits or len(digits) > 5 or not is_akshare_available():
            return []
        code = normalize_hk_code(digits)
        ak = _akshare_import()
        if ak is None:
            return []
        try:
            df = _akshare_call(
                lambda: ak.stock_hk_security_profile_em(symbol=code),
                label=f"港股直通({code})",
            )
            if df is None or df.empty:
                return []
            name = str(df.iloc[0].get("证券简称") or "").strip()
            if not name:
                return []
            return [
                {
                    "code": code,
                    "name": name,
                    "market": MARKET_HK,
                    "initials": _pinyin_initials(name),
                }
            ]
        except Exception as exc:
            logger.debug("港股代码直通失败 %s: %s", code, exc)
            return []

    def search(self, q: str, *, limit: int = 20, refresh: bool = False) -> dict[str, Any]:
        query = (q or "").strip()
        if not query:
            return {"ok": True, "query": "", "items": [], "total": 0}

        limit = max(1, min(int(limit), 100))
        items, meta = self._get_items(refresh=refresh)
        if not items:
            return {
                "ok": False,
                "query": query,
                "items": [],
                "total": 0,
                "error": meta.get("error") or "检索索引不可用",
            }

        q_lower = query.lower()
        scored: list[tuple[int, dict[str, Any]]] = []
        pool = list(items)
        if self._hk_enabled():
            has_hk = any((row.get("market") or "").upper() == MARKET_HK for row in pool)
            if not has_hk and _hk_lookup_cache:
                pool.extend(list(_hk_lookup_cache[1]))
        if self._us_enabled():
            has_us = any((row.get("market") or "").upper() == MARKET_US for row in pool)
            if not has_us and _us_lookup_cache:
                pool.extend(list(_us_lookup_cache[1]))

        for item in pool:
            score = self._match_score(item, query, q_lower)
            if score > 0:
                scored.append((score, item))

        if self._hk_enabled():
            import re

            digits = re.sub(r"\D", "", query)
            if digits and len(digits) <= 5:
                for item in self._lookup_hk_by_code(query):
                    score = self._match_score(item, query, q_lower)
                    if score > 0:
                        scored.append((score, item))
            elif not scored and _hk_lookup_cache and any("\u4e00" <= ch <= "\u9fff" for ch in query):
                for item in _hk_lookup_cache[1]:
                    score = self._match_score(item, query, q_lower)
                    if score > 0:
                        scored.append((score, item))

        if self._us_enabled():
            from src.core.stock_market_utils import is_us_ticker

            if is_us_ticker(query):
                for item in self._lookup_us_by_code(query):
                    score = self._match_score(item, query, q_lower)
                    if score > 0:
                        scored.append((score, item))
            elif not scored and _us_lookup_cache and query.isascii():
                for item in _us_lookup_cache[1]:
                    score = self._match_score(item, query, q_lower)
                    if score > 0:
                        scored.append((score, item))

        scored.sort(key=lambda pair: (-pair[0], pair[1].get("code", "")))
        hits = [
            {
                "code": row["code"],
                "name": row["name"],
                "market": row.get("market") or _market_from_code(row["code"]),
            }
            for _, row in scored[:limit]
        ]
        return {
            "ok": True,
            "query": query,
            "items": hits,
            "total": len(scored),
            "index_updated_at": meta.get("updated_at"),
            "index_count": meta.get("count"),
        }


def warm_hk_search_cache() -> None:
    """后台预热港股检索缓存（不阻塞请求）。"""
    try:
        if is_hk_enabled():
            get_stock_search_service()._lookup_hk_items()
    except Exception as exc:
        logger.debug("港股检索缓存预热失败: %s", exc)


def warm_us_search_cache() -> None:
    """后台预热美股检索缓存（不阻塞请求）。"""
    try:
        if is_us_enabled():
            get_stock_search_service()._lookup_us_items()
    except Exception as exc:
        logger.debug("美股检索缓存预热失败: %s", exc)


def warm_overseas_search_cache() -> None:
    warm_hk_search_cache()
    warm_us_search_cache()


_service: StockSearchService | None = None


def get_stock_search_service() -> StockSearchService:
    global _service
    if _service is None:
        _service = StockSearchService()
    return _service
