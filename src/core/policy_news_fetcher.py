"""政府政策资讯采集：部委官网 HTML/RSS + mock 降级。"""

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
from urllib.parse import urljoin, urlparse

import httpx

from src.core.config import get_settings
from src.core.policy_sources import (
    load_policy_sources_config,
    matched_policy_keywords,
    matched_policy_themes,
    use_policy_mock_when_empty,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
POLICY_NEWS_DIR = ROOT / "data" / "policy_news"
POLICY_NEWS_INDEX = POLICY_NEWS_DIR / "index.json"

DISCLAIMER = (
    "政策解读仅供参考，不构成任何投资建议。"
    "投资有风险，决策请谨慎。"
)

_LINK_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{4,200})</a>',
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})")


def _policy_id(url: str, title: str) -> str:
    key = f"{url}|{title}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


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


def _parse_cn_date(text: str) -> Optional[datetime]:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1).replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _normalize_item(
    *,
    title: str,
    url: str,
    source_id: str,
    source_name: str,
    published_at: Optional[datetime] = None,
    summary: str = "",
    themes: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
    origin: str = "fetch",
) -> dict[str, Any]:
    now = datetime.now()
    text = f"{title}\n{summary}"
    kws = keywords or matched_policy_keywords(text)
    ths = themes or matched_policy_themes(text)
    return {
        "id": _policy_id(url, title),
        "title": title.strip(),
        "url": url.strip(),
        "source_id": source_id,
        "source_name": source_name,
        "summary": summary[:2000] if summary else "",
        "keywords": kws,
        "themes": ths,
        "published_at": (published_at or now).isoformat(),
        "fetched_at": now.isoformat(),
        "origin": origin,
    }


def _parse_policy_sources_env() -> list[str]:
    raw = os.getenv("POLICY_NEWS_SOURCES", "").strip()
    if not raw:
        cfg = load_policy_sources_config()
        defaults = cfg.get("default_sources") or []
        if defaults:
            return [str(s).strip() for s in defaults if str(s).strip()]
        return ["gov_cn", "ndrc", "miit", "most", "moa", "mee"]
    return [s.strip() for s in raw.split(",") if s.strip()]


class PolicyNewsFetcher:
    """拉取、关键词过滤、去重、持久化政府政策资讯。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._cfg = load_policy_sources_config()
        self._interval = float(self._cfg.get("request_interval_seconds") or 3)
        self._max_per_source = int(self._cfg.get("max_items_per_source") or 15)
        self._timeout = float(self._cfg.get("timeout_seconds") or 25)
        self._match_keywords = [
            str(k).strip() for k in (self._cfg.get("match_keywords") or []) if str(k).strip()
        ]

    @property
    def enabled_sources(self) -> list[str]:
        return _parse_policy_sources_env()

    def _http_client(self) -> httpx.Client:
        proxy = self._settings.https_proxy or self._settings.http_proxy
        return httpx.Client(
            timeout=self._timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; web_scraper-policy/1.0; "
                    "+research; respectful crawl)"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            proxy=proxy or None,
        )

    def _source_defs(self) -> list[dict[str, Any]]:
        yaml_sources = self._cfg.get("sources") or []
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
                defs.append({"id": sid, "type": sid})
        return defs

    def _matches_keywords(self, text: str) -> bool:
        if not self._match_keywords:
            return True
        return any(kw in text for kw in self._match_keywords)

    def fetch_mock(self) -> list[dict[str, Any]]:
        now = datetime.now()
        items: list[dict[str, Any]] = []
        mock_items = self._cfg.get("mock_items") or []
        for i, raw in enumerate(mock_items[: self._max_per_source]):
            if not isinstance(raw, dict):
                continue
            item = _normalize_item(
                title=str(raw.get("title") or ""),
                url=str(raw.get("url") or f"mock://policy/{i}"),
                source_id="mock",
                source_name=str(raw.get("source_name") or "演示政策"),
                summary=str(raw.get("summary") or ""),
                themes=[str(t) for t in (raw.get("themes") or [])],
                published_at=now - timedelta(hours=i * 4),
                origin="mock",
            )
            if item["title"]:
                items.append(item)
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
            logger.warning("政策 RSS 抓取失败 %s: %s", url, exc)
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for entry in entries[: self._max_per_source * 2]:
            title_el = entry.find("title") or entry.find("atom:title", ns)
            link_el = entry.find("link") or entry.find("atom:link", ns)
            desc_el = (
                entry.find("description")
                or entry.find("summary")
                or entry.find("atom:summary", ns)
            )
            date_el = (
                entry.find("pubDate")
                or entry.find("published")
                or entry.find("atom:published", ns)
            )
            title = _strip_html(title_el.text if title_el is not None else "")
            if not title or not self._matches_keywords(title):
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
            if len(items) >= self._max_per_source:
                break
        return items

    def fetch_html_list(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        url = str(source.get("url") or "")
        sid = str(source.get("id") or "html")
        sname = str(source.get("name") or sid)
        domain = str(source.get("domain") or urlparse(url).netloc)
        href_contains = str(source.get("link_href_contains") or "")
        if not url:
            return []

        items: list[dict[str, Any]] = []
        try:
            with self._http_client() as client:
                resp = client.get(url)
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:
            logger.warning("政策 HTML 列表抓取失败 %s: %s", url, exc)
            return []

        seen_urls: set[str] = set()
        for match in _LINK_RE.finditer(html):
            href, raw_title = match.group(1), match.group(2)
            title = _strip_html(raw_title)
            if len(title) < 6:
                continue
            full_url = urljoin(url, href)
            if href_contains and href_contains not in href and href_contains not in full_url:
                continue
            if not self._matches_keywords(title):
                continue
            if domain and domain not in full_url:
                parsed = urlparse(full_url)
                if parsed.netloc and domain not in parsed.netloc:
                    continue
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            pub = _parse_cn_date(match.group(0))
            items.append(
                _normalize_item(
                    title=title,
                    url=full_url,
                    source_id=sid,
                    source_name=sname,
                    summary="",
                    published_at=pub,
                    origin="html_list",
                )
            )
            if len(items) >= self._max_per_source:
                break
        return items

    def fetch_all(self, *, days: int = 30) -> dict[str, Any]:
        all_items: list[dict[str, Any]] = []
        source_stats: dict[str, int] = {}
        errors: list[str] = []

        for src in self._source_defs():
            stype = str(src.get("type") or src.get("id") or "")
            sid = str(src.get("id") or stype)
            try:
                if stype == "mock":
                    batch = self.fetch_mock()
                elif stype == "rss":
                    batch = self.fetch_rss(src)
                elif stype == "html_list":
                    batch = self.fetch_html_list(src)
                else:
                    errors.append(f"未知政策源类型: {stype}")
                    continue
                source_stats[sid] = len(batch)
                all_items.extend(batch)
                if stype in ("rss", "html_list"):
                    time.sleep(self._interval)
            except Exception as exc:
                logger.exception("政策源 %s 失败", sid)
                errors.append(f"{sid}: {exc}")

        if not all_items and use_policy_mock_when_empty():
            mock_batch = self.fetch_mock()
            source_stats["mock"] = len(mock_batch)
            all_items.extend(mock_batch)

        deduped = self._dedupe(all_items)
        saved = self._save_items(deduped)
        real_count = sum(v for k, v in source_stats.items() if k != "mock")
        degraded = bool(errors) or real_count == 0
        return {
            "ok": True,
            "fetched": len(all_items),
            "deduped": len(deduped),
            "saved_new": saved,
            "source_stats": source_stats,
            "enabled_sources": self.enabled_sources,
            "errors": errors,
            "degraded": degraded,
            "used_mock": source_stats.get("mock", 0) > 0 and real_count == 0,
            "disclaimer": DISCLAIMER,
        }

    def _dedupe(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            nid = item.get("id") or _policy_id(item.get("url", ""), item.get("title", ""))
            if nid in seen:
                continue
            seen.add(nid)
            out.append(item)
        return out

    def _load_index(self) -> dict[str, Any]:
        if not POLICY_NEWS_INDEX.exists():
            return {"ids": [], "updated_at": None}
        try:
            return json.loads(POLICY_NEWS_INDEX.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ids": [], "updated_at": None}

    def _save_index(self, ids: list[str]) -> None:
        POLICY_NEWS_DIR.mkdir(parents=True, exist_ok=True)
        POLICY_NEWS_INDEX.write_text(
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

        POLICY_NEWS_DIR.mkdir(parents=True, exist_ok=True)
        day_file = POLICY_NEWS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
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
            coll = client[self._settings.mongodb_db]["policy_news"]
            for item in items:
                coll.update_one({"id": item["id"]}, {"$set": item}, upsert=True)
        except Exception as exc:
            logger.warning("MongoDB policy_news 写入失败: %s", exc)

    def _load_from_mongo(self, *, limit: int, days: int) -> list[dict[str, Any]]:
        uri = self._settings.mongodb_uri
        if not uri:
            return []
        cutoff = datetime.now() - timedelta(days=days)
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            coll = client[self._settings.mongodb_db]["policy_news"]
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
            logger.warning("MongoDB policy_news 读取失败: %s", exc)
            return []

    def _load_from_jsonl(self, *, limit: int, days: int) -> list[dict[str, Any]]:
        if not POLICY_NEWS_DIR.is_dir():
            return []
        cutoff = datetime.now() - timedelta(days=days)
        items: list[dict[str, Any]] = []
        files = sorted(POLICY_NEWS_DIR.glob("*.jsonl"), reverse=True)
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
        days: int = 30,
        theme: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        days = max(1, min(days, 90))
        items = self._load_from_mongo(limit=limit, days=days)
        storage = "mongodb" if items else "jsonl"
        if not items:
            items = self._load_from_jsonl(limit=limit, days=days)

        if not use_policy_mock_when_empty():
            items = [i for i in items if i.get("source_id") != "mock"]

        if not items and use_policy_mock_when_empty():
            items = self.fetch_mock()[:limit]
            storage = "mock"

        if theme:
            th = theme.strip()
            items = [
                i
                for i in items
                if th in (i.get("themes") or [])
                or th in (i.get("keywords") or [])
                or th in (i.get("title") or "")
            ]
        if source_id:
            sid = source_id.strip()
            items = [i for i in items if i.get("source_id") == sid]

        return {
            "total": len(items),
            "items": items[:limit],
            "days": days,
            "theme_filter": theme,
            "source_filter": source_id,
            "storage": storage,
            "enabled_sources": self.enabled_sources,
            "disclaimer": DISCLAIMER,
        }


_fetcher: Optional[PolicyNewsFetcher] = None


def get_policy_news_fetcher() -> PolicyNewsFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = PolicyNewsFetcher()
    return _fetcher
