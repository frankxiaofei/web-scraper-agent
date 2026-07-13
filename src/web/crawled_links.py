"""从 crawl run events 或 Mongo 提取爬取链接清单。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.core.config import get_settings
from src.db.mongo_repository import create_mongo_repository


def _link_from_dict(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    url = (item.get("url") or item.get("detail_url") or "").strip()
    if not url:
        return None
    title = (item.get("title") or url).strip()
    publish_date = item.get("publish_date")
    if publish_date is not None and not isinstance(publish_date, str):
        publish_date = str(publish_date)
    return {"title": title, "url": url, "publish_date": publish_date}


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _merge_link(
    links: list[dict[str, Any]],
    seen: set[str],
    item: dict[str, Any],
) -> None:
    link = _link_from_dict(item)
    if not link or link["url"] in seen:
        return
    seen.add(link["url"])
    links.append(link)


def extract_links_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 run events 提取链接，优先 save/parse 中的 urls 列表。"""
    links: list[dict[str, Any]] = []
    seen: set[str] = set()

    priority_types = ("save", "parse", "fetch_detail")
    for event_type in priority_types:
        for ev in events:
            if ev.get("type") != event_type:
                continue
            data = ev.get("data") or {}
            for key in ("urls", "notices", "links"):
                raw = data.get(key)
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict):
                            _merge_link(links, seen, item)

            if event_type == "fetch_detail":
                url = data.get("url") or ev.get("url")
                if url:
                    _merge_link(links, seen, {"url": url, "title": data.get("title") or url})

    return links


def _mongo_fallback_links(run: dict[str, Any]) -> list[dict[str, Any]]:
    limit = int(run.get("notices_count") or 0)
    if limit <= 0:
        return []

    site_id = run.get("site_id") or ""
    if not site_id:
        return []

    settings = get_settings()
    repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    try:
        if not repo._available or repo._notices is None:
            return []

        started = _parse_iso_datetime(run.get("started_at"))
        finished = _parse_iso_datetime(run.get("finished_at")) or started
        site_filter = {
            "$or": [
                {"source": site_id},
                {"site_id": site_id},
                {"source_site_id": site_id},
            ]
        }

        if started and finished:
            window_start = started - timedelta(seconds=30)
            window_end = finished + timedelta(minutes=10)
            time_query = {
                "$and": [
                    site_filter,
                    {"crawled_at": {"$gte": window_start, "$lte": window_end}},
                ]
            }
            cursor = (
                repo._notices.find(time_query)
                .sort("crawled_at", -1)
                .limit(limit)
            )
            docs = list(cursor)
            if docs:
                return [_doc_to_link(d) for d in docs]

        cursor = (
            repo._notices.find(site_filter)
            .sort("crawled_at", -1)
            .limit(limit)
        )
        return [_doc_to_link(d) for d in cursor]
    finally:
        repo.close()


def _doc_to_link(doc: dict[str, Any]) -> dict[str, Any]:
    url = doc.get("url") or doc.get("detail_url") or ""
    title = doc.get("title") or url
    publish_date = doc.get("publish_date")
    if publish_date is not None and not isinstance(publish_date, str):
        publish_date = str(publish_date)
    return {"title": title, "url": url, "publish_date": publish_date}


def get_crawled_links(
    run: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    links = extract_links_from_events(events)
    if links:
        return links
    return _mongo_fallback_links(run)
