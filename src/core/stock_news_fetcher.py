"""股票/财经新闻资讯采集：AKShare、RSS、bid_notices 关键词、mock。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx

from src.core.config import get_settings
from src.core.stock_market_data import get_data_provider, is_akshare_available
from src.core.stock_watch import (
    get_industries,
    get_stock_match_keywords,
    get_stock_themes,
    is_stock_related,
    load_stock_watch_config,
    matched_stock_keywords,
    matched_stock_themes,
)
from src.web.data_service import doc_publish_timestamp, doc_source_id

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
STOCK_NEWS_DIR = ROOT / "data" / "stock_news"
STOCK_NEWS_INDEX = STOCK_NEWS_DIR / "index.json"

DISCLAIMER = "本平台内容仅供参考，不构成任何投资建议。投资有风险，决策请谨慎。"

_MOCK_NEWS: list[dict[str, Any]] = [
    {
        "title": "新能源产业链：储能装机量持续高增，政策支持力度加大",
        "url": "https://example.com/news/mock-new-energy",
        "source_id": "mock",
        "source_name": "演示财经",
        "category": "产业",
        "industries": ["新能源"],
        "summary": "多地出台储能补贴与绿电交易细则，产业链上游材料与中游设备厂商订单饱满。",
    },
    {
        "title": "半导体设备国产化率提升，先进封装产能扩张",
        "url": "https://example.com/news/mock-semiconductor",
        "source_id": "mock",
        "source_name": "演示财经",
        "category": "产业",
        "industries": ["半导体"],
        "summary": "国产刻蚀、薄膜沉积设备验证进度加快，封测环节资本开支维持高位。",
    },
    {
        "title": "央企改革深化：多家央企控股上市公司发布重组进展公告",
        "url": "https://example.com/news/mock-soe-reform",
        "source_id": "mock",
        "source_name": "演示财经",
        "category": "政策",
        "industries": ["央企改革"],
        "summary": "专业化整合与市值管理考核机制推进，关注资产注入与同业竞争解决。",
    },
    {
        "title": "智慧农业：数字乡村与农业物联网示范项目加速落地",
        "url": "https://example.com/news/mock-smart-agri",
        "source_id": "mock",
        "source_name": "演示财经",
        "category": "产业",
        "industries": ["智慧农业"],
        "summary": "精准灌溉、遥感监测、溯源系统在政府采购与示范项目中的渗透率提升。",
    },
]


def _parse_stock_news_rss_env() -> list[dict[str, Any]]:
    """从 STOCK_NEWS_RSS 解析额外 RSS 源。"""
    raw = os.getenv("STOCK_NEWS_RSS", "").strip()
    if not raw:
        settings = get_settings()
        raw = (settings.stock_news_rss or "").strip()
    if not raw:
        return []
    sources: list[dict[str, Any]] = []
    for i, url in enumerate(raw.split(",")):
        url = url.strip()
        if not url:
            continue
        sid = f"rss_env_{i}"
        sources.append({"id": sid, "type": "rss", "name": f"RSS-{i + 1}", "url": url})
    return sources


def _use_stock_mock() -> bool:
    raw = os.getenv("STOCK_USE_MOCK_WHEN_EMPTY", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    cfg = load_stock_watch_config()
    news_cfg = cfg.get("news") or {}
    return bool(news_cfg.get("use_mock_when_empty", False))


def _default_sources_for_provider() -> list[str]:
    if is_akshare_available():
        return ["akshare", "bid_notices"]
    return ["bid_notices"]


def _parse_stock_news_sources_env() -> list[str]:
    raw = os.getenv("STOCK_NEWS_SOURCES", "").strip()
    if not raw:
        settings = get_settings()
        raw = (settings.stock_news_sources or "").strip()
    if not raw:
        cfg = load_stock_watch_config()
        news_cfg = cfg.get("news") or {}
        if news_cfg.get("default_sources"):
            return [str(s).strip() for s in news_cfg["default_sources"] if str(s).strip()]
        return _default_sources_for_provider()
    return [s.strip() for s in raw.split(",") if s.strip()]


def _news_id(url: str, title: str) -> str:
    key = f"{url}|{title}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]


def _parse_cn_datetime(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if " " in fmt else text[:10], fmt)
        except ValueError:
            continue
    return _parse_rss_date(text)


def _parse_rss_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text.replace("GMT", "+0000"), fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _match_industries(text: str) -> list[str]:
    if not text.strip():
        return []
    found: list[str] = []
    upper = text.upper()
    for ind in get_industries():
        name = str(ind.get("name") or ind.get("id") or "")
        for kw in ind.get("keywords") or []:
            kw_str = str(kw).strip()
            if kw_str and (kw_str.upper() in upper or kw_str in text):
                if name and name not in found:
                    found.append(name)
                break
    return found


def _normalize_item(
    *,
    title: str,
    url: str,
    source_id: str,
    source_name: str,
    published_at: Optional[datetime] = None,
    summary: str = "",
    category: str = "资讯",
    industries: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
    origin: str = "fetch",
) -> dict[str, Any]:
    now = datetime.now()
    text = f"{title}\n{summary}"
    inds = industries or _match_industries(text)
    kws = keywords or []
    if not kws:
        kws = matched_stock_keywords({"title": title, "content_text": summary})
    return {
        "id": _news_id(url, title),
        "title": title.strip(),
        "url": url.strip(),
        "source_id": source_id,
        "source_name": source_name,
        "category": category,
        "summary": summary[:2000] if summary else "",
        "industries": inds,
        "keywords": kws,
        "themes": matched_stock_themes({"title": title, "content_text": summary}),
        "published_at": (published_at or now).isoformat(),
        "fetched_at": now.isoformat(),
        "origin": origin,
    }


class StockNewsFetcher:
    """拉取、去重、持久化股票/财经新闻。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._cfg = load_stock_watch_config()
        self._news_cfg = self._cfg.get("news") or {}
        self._interval = float(self._news_cfg.get("request_interval_seconds") or 2)
        self._max_per_source = int(self._news_cfg.get("max_items_per_source") or 20)

    @property
    def enabled_sources(self) -> list[str]:
        return _parse_stock_news_sources_env()

    def _http_client(self) -> httpx.Client:
        proxy = self._settings.https_proxy or self._settings.http_proxy
        return httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "web_scraper-stock-news/1.0 (+research; respectful crawl)"},
            proxy=proxy or None,
        )

    def _source_defs(self) -> list[dict[str, Any]]:
        yaml_sources = self._news_cfg.get("sources") or []
        enabled = set(self.enabled_sources)
        defs: list[dict[str, Any]] = []
        for src in yaml_sources:
            if not isinstance(src, dict):
                continue
            sid = str(src.get("id") or "")
            if sid and sid in enabled:
                if src.get("enabled") is False:
                    continue
                defs.append(src)
        for sid in enabled:
            if sid and not any(str(d.get("id")) == sid for d in defs):
                if sid == "akshare":
                    defs.append({"id": "akshare", "type": "akshare", "name": "东方财富财经快讯"})
                else:
                    defs.append({"id": sid, "type": sid})
        defs.extend(_parse_stock_news_rss_env())
        return defs

    def fetch_mock(self) -> list[dict[str, Any]]:
        now = datetime.now()
        items: list[dict[str, Any]] = []
        for i, raw in enumerate(_MOCK_NEWS[: self._max_per_source]):
            item = _normalize_item(
                title=raw["title"],
                url=raw["url"],
                source_id="mock",
                source_name=raw.get("source_name") or "演示财经",
                summary=raw.get("summary") or "",
                category=raw.get("category") or "资讯",
                industries=raw.get("industries"),
                published_at=now - timedelta(hours=i * 3),
                origin="mock",
            )
            items.append(item)
        return items

    def fetch_bid_notices(self, *, days: int = 7) -> list[dict[str, Any]]:
        from src.web.data_service import get_data_service

        days = max(1, min(days, 90))
        cutoff = datetime.now() - timedelta(days=days)
        svc = get_data_service()
        docs: list[dict[str, Any]] = []

        if svc.data_source == "mongodb" and svc._mongo_coll is not None:
            mvp_ids = svc._enabled_site_ids()
            if not mvp_ids:
                return []
            coll = svc._mongo_coll
            query: dict[str, Any] = svc._mvp_source_filter_mongo(mvp_ids)
            cursor = coll.find(query).sort(
                [("scraped_at", -1), ("crawled_at", -1), ("created_at", -1)]
            )
            docs = list(cursor)
        else:
            for doc in svc._load_jsonl():  # noqa: SLF001
                if not svc._in_mvp_scope(doc):  # noqa: SLF001
                    continue
                docs.append(doc)

        items: list[dict[str, Any]] = []
        for doc in docs:
            if not is_stock_related(doc):
                continue
            ts = doc_publish_timestamp(doc)
            if ts is not None and ts < cutoff:
                continue
            title = str(doc.get("title") or "")
            url = str(doc.get("url") or "")
            if not title:
                continue
            summary = str(doc.get("content_text") or "")[:500]
            items.append(
                _normalize_item(
                    title=title,
                    url=url or f"bid://{doc_source_id(doc)}/{_news_id(title, url)}",
                    source_id="bid_notices",
                    source_name=str(doc.get("source_site_name") or "招采公告库"),
                    summary=summary,
                    category=str(doc.get("category") or "公告"),
                    keywords=matched_stock_keywords(doc),
                    industries=_match_industries(f"{title}\n{summary}"),
                    published_at=ts,
                    origin="bid_notices",
                )
            )
            if len(items) >= self._max_per_source:
                break
        return items

    def fetch_rss(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        url = str(source.get("url") or "")
        sid = str(source.get("id") or "rss")
        sname = str(source.get("name") or sid)
        if not url:
            return []
        items: list[dict[str, Any]] = []
        try:
            with self._http_client() as client:
                resp = client.get(url)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
        except Exception as exc:
            logger.warning("RSS 抓取失败 %s: %s", url, exc)
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for entry in entries[: self._max_per_source]:
            title_el = entry.find("title") or entry.find("atom:title", ns)
            link_el = entry.find("link") or entry.find("atom:link", ns)
            desc_el = entry.find("description") or entry.find("summary") or entry.find("atom:summary", ns)
            date_el = entry.find("pubDate") or entry.find("published") or entry.find("atom:published", ns)

            title = _strip_html(title_el.text if title_el is not None else "")
            if not title:
                continue
            link = ""
            if link_el is not None:
                link = link_el.get("href") or (link_el.text or "")
            summary = _strip_html(desc_el.text if desc_el is not None else "")
            pub = _parse_rss_date(date_el.text if date_el is not None else None)
            items.append(
                _normalize_item(
                    title=title,
                    url=link or url,
                    source_id=sid,
                    source_name=sname,
                    summary=summary,
                    published_at=pub,
                    origin="rss",
                )
            )
        return items

    def fetch_akshare(self) -> list[dict[str, Any]]:
        """AKShare 东方财富全球财经快讯（稳定接口 stock_info_global_em）。"""
        if not is_akshare_available():
            logger.warning("AKShare 未安装，跳过 akshare 新闻源")
            return []
        try:
            import akshare as ak  # type: ignore
        except ImportError:
            return []

        items: list[dict[str, Any]] = []
        try:
            df = ak.stock_info_global_em()
        except Exception as exc:
            logger.warning("AKShare stock_info_global_em 失败: %s", exc)
            return self._fetch_akshare_watch_symbol_news(ak)

        col_map = {str(c): c for c in df.columns}
        title_col = col_map.get("标题") or col_map.get("title")
        summary_col = col_map.get("摘要") or col_map.get("内容") or col_map.get("summary")
        time_col = col_map.get("发布时间") or col_map.get("时间") or col_map.get("publish_time")
        url_col = col_map.get("链接") or col_map.get("url")

        for _, row in df.head(self._max_per_source).iterrows():
            title = str(row[title_col] if title_col else row.iloc[0] if len(row) else "")
            if not title.strip():
                continue
            summary = str(row[summary_col] if summary_col else "")
            link = str(row[url_col] if url_col else "")
            pub: Optional[datetime] = None
            if time_col is not None:
                raw_time = str(row[time_col])
                pub = _parse_rss_date(raw_time) or _parse_cn_datetime(raw_time)
            items.append(
                _normalize_item(
                    title=title,
                    url=link or f"akshare://global/{_news_id(title, summary)}",
                    source_id="akshare",
                    source_name="东方财富全球快讯",
                    summary=summary,
                    category="财经",
                    published_at=pub,
                    origin="akshare",
                )
            )
        if not items:
            items = self._fetch_akshare_watch_symbol_news(ak)
        return items

    def _fetch_akshare_watch_symbol_news(self, ak: Any) -> list[dict[str, Any]]:
        """降级：从关注代码拉取个股新闻。"""
        from src.core.stock_watch import get_watch_symbols

        items: list[dict[str, Any]] = []
        for sym in get_watch_symbols()[:3]:
            code = str(sym.get("code") or "").strip()
            if not code or not code.isdigit():
                continue
            try:
                df = ak.stock_news_em(symbol=code)
            except Exception as exc:
                logger.debug("AKShare stock_news_em %s 失败: %s", code, exc)
                continue
            for _, row in df.head(5).iterrows():
                title = str(row.get("新闻标题") or row.get("title") or "")
                if not title:
                    continue
                pub_raw = str(row.get("发布时间") or row.get("publish_time") or "")
                items.append(
                    _normalize_item(
                        title=title,
                        url=str(row.get("新闻链接") or row.get("url") or f"akshare://news/{code}"),
                        source_id="akshare",
                        source_name=f"东方财富-{sym.get('name') or code}",
                        summary=str(row.get("新闻内容") or row.get("content") or "")[:500],
                        category="个股",
                        published_at=_parse_cn_datetime(pub_raw),
                        origin="akshare",
                    )
                )
            if len(items) >= self._max_per_source:
                break
        return items[: self._max_per_source]

    def fetch_all(self, *, days: int = 7) -> dict[str, Any]:
        all_items: list[dict[str, Any]] = []
        source_stats: dict[str, int] = {}
        errors: list[str] = []

        for src in self._source_defs():
            stype = str(src.get("type") or src.get("id") or "")
            sid = str(src.get("id") or stype)
            try:
                if stype == "mock":
                    batch = self.fetch_mock()
                elif stype == "bid_notices":
                    batch = self.fetch_bid_notices(days=days)
                elif stype == "akshare":
                    batch = self.fetch_akshare()
                elif stype == "rss":
                    batch = self.fetch_rss(src)
                else:
                    errors.append(f"未知新闻源类型: {stype}")
                    continue
                source_stats[sid] = len(batch)
                all_items.extend(batch)
                if stype == "rss":
                    time.sleep(self._interval)
            except Exception as exc:
                logger.exception("新闻源 %s 失败", sid)
                errors.append(f"{sid}: {exc}")

        deduped = self._dedupe(all_items)
        saved = self._save_items(deduped)
        degraded = bool(errors) or (
            "akshare" in self.enabled_sources and source_stats.get("akshare", 0) == 0
        )
        return {
            "ok": True,
            "fetched": len(all_items),
            "deduped": len(deduped),
            "saved_new": saved,
            "source_stats": source_stats,
            "enabled_sources": self.enabled_sources,
            "errors": errors,
            "degraded": degraded,
            "disclaimer": DISCLAIMER,
        }

    def _dedupe(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            nid = item.get("id") or _news_id(item.get("url", ""), item.get("title", ""))
            if nid in seen:
                continue
            seen.add(nid)
            out.append(item)
        return out

    def _load_index(self) -> dict[str, Any]:
        if not STOCK_NEWS_INDEX.exists():
            return {"ids": [], "updated_at": None}
        try:
            return json.loads(STOCK_NEWS_INDEX.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ids": [], "updated_at": None}

    def _save_index(self, ids: list[str]) -> None:
        STOCK_NEWS_DIR.mkdir(parents=True, exist_ok=True)
        STOCK_NEWS_INDEX.write_text(
            json.dumps(
                {"ids": ids[-5000:], "updated_at": datetime.now().isoformat()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _save_items(self, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        index = self._load_index()
        known = set(index.get("ids") or [])
        new_items = [i for i in items if i.get("id") not in known]
        if not new_items:
            return 0

        STOCK_NEWS_DIR.mkdir(parents=True, exist_ok=True)
        day_file = STOCK_NEWS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with day_file.open("a", encoding="utf-8") as f:
            for item in new_items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

        self._save_to_mongo(new_items)

        for item in new_items:
            known.add(item["id"])
        self._save_index(list(known))
        return len(new_items)

    def _save_to_mongo(self, items: list[dict[str, Any]]) -> None:
        uri = self._settings.mongodb_uri
        if not uri:
            return
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            coll = client[self._settings.mongodb_db]["stock_news"]
            for item in items:
                coll.update_one({"id": item["id"]}, {"$set": item}, upsert=True)
        except Exception as exc:
            logger.warning("MongoDB stock_news 写入失败: %s", exc)

    def _load_from_mongo(self, *, limit: int, days: int) -> list[dict[str, Any]]:
        uri = self._settings.mongodb_uri
        if not uri:
            return []
        cutoff = datetime.now() - timedelta(days=days)
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            coll = client[self._settings.mongodb_db]["stock_news"]
            docs = list(
                coll.find({}).sort([("published_at", -1), ("fetched_at", -1)]).limit(limit * 3)
            )
            out: list[dict[str, Any]] = []
            for doc in docs:
                doc.pop("_id", None)
                pub = doc.get("published_at")
                if isinstance(pub, str):
                    try:
                        if datetime.fromisoformat(pub) < cutoff:
                            continue
                    except ValueError:
                        pass
                out.append(doc)
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:
            logger.warning("MongoDB stock_news 读取失败: %s", exc)
            return []

    def _load_from_jsonl(self, *, limit: int, days: int) -> list[dict[str, Any]]:
        if not STOCK_NEWS_DIR.is_dir():
            return []
        cutoff = datetime.now() - timedelta(days=days)
        items: list[dict[str, Any]] = []
        files = sorted(STOCK_NEWS_DIR.glob("*.jsonl"), reverse=True)
        for fp in files:
            try:
                lines = fp.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pub = item.get("published_at")
                if isinstance(pub, str):
                    try:
                        if datetime.fromisoformat(pub) < cutoff:
                            continue
                    except ValueError:
                        pass
                items.append(item)
                if len(items) >= limit:
                    return items
        return items

    def list_news(
        self,
        *,
        limit: int = 30,
        days: int = 7,
        industry: Optional[str] = None,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        days = max(1, min(days, 90))
        items = self._load_from_mongo(limit=limit, days=days)
        storage = "mongodb" if items else "jsonl"
        if not items:
            items = self._load_from_jsonl(limit=limit, days=days)

        if "mock" not in self.enabled_sources and not _use_stock_mock():
            items = [i for i in items if i.get("source_id") != "mock"]

        if industry:
            ind = industry.strip()
            items = [
                i
                for i in items
                if ind in (i.get("industries") or [])
                or ind in (i.get("keywords") or [])
                or ind in (i.get("title") or "")
            ]

        return {
            "total": len(items),
            "items": items[:limit],
            "days": days,
            "industry_filter": industry,
            "storage": storage,
            "enabled_sources": self.enabled_sources,
            "disclaimer": DISCLAIMER,
        }


_fetcher: Optional[StockNewsFetcher] = None


def get_stock_news_fetcher() -> StockNewsFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = StockNewsFetcher()
    return _fetcher
