"""从 crawl run events 增量提取 URL 爬取进度（供 Agent SSE 推送）。"""

from __future__ import annotations

import re
from typing import Any

_DETAIL_PROGRESS_RE = re.compile(r"抓取详情页\s*\((\d+)/(\d+)\)")


def _event_time(ev: dict[str, Any]) -> str:
    ts = ev.get("timestamp") or ""
    text = str(ts)
    if not text:
        return ""
    return text[:19].replace("T", " ")


def _links_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("urls", "notices", "links"):
        raw = data.get(key)
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict) and (x.get("url") or "").strip()]
    return []


class UrlCrawlTracker:
    """对比已见 URL，从 run events 增量产出 url_crawl payload。"""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}

    def process_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        emitted: list[dict[str, Any]] = []
        for ev in events:
            emitted.extend(self._process_one(ev))
        return emitted

    def summary(self) -> dict[str, Any]:
        items = list(self._states.values())
        items.sort(key=lambda x: x.get("index") or 0)
        by_status: dict[str, int] = {}
        for item in items:
            st = str(item.get("status") or "unknown")
            by_status[st] = by_status.get(st, 0) + 1
        return {
            "total": len(items),
            "by_status": by_status,
            "recent": items[-10:],
        }

    def _process_one(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        etype = ev.get("type") or ""
        data = ev.get("data") or {}
        message = ev.get("message") or ""
        ts = _event_time(ev)
        emitted: list[dict[str, Any]] = []

        if etype == "fetch_detail":
            url = (data.get("url") or ev.get("url") or "").strip()
            if not url:
                return emitted
            page, total = None, None
            match = _DETAIL_PROGRESS_RE.search(message)
            if match:
                page, total = int(match.group(1)), int(match.group(2))
            item = self._upsert(
                url,
                status="fetching",
                title=data.get("title") or url,
                page=page,
                total=total,
                message=message,
                time=ts,
            )
            if item:
                emitted.append(item)
            return emitted

        if etype == "parse":
            for link in _links_from_data(data):
                url = (link.get("url") or "").strip()
                if not url:
                    continue
                prev = self._states.get(url)
                status = "saved" if prev is None else (prev.get("status") or "fetching")
                item = self._upsert(
                    url,
                    status=status,
                    title=link.get("title") or url,
                    message=message,
                    time=ts or (prev or {}).get("time", ""),
                )
                if item:
                    emitted.append(item)
            return emitted

        if etype == "save":
            for link in _links_from_data(data):
                url = (link.get("url") or "").strip()
                if not url:
                    continue
                item = self._upsert(
                    url,
                    status="saved",
                    title=link.get("title") or url,
                    message=message,
                    time=ts,
                )
                if item:
                    emitted.append(item)
            return emitted

        if etype == "error" and data.get("url"):
            url = str(data.get("url")).strip()
            item = self._upsert(
                url,
                status="failed",
                title=data.get("title") or url,
                message=message or str(data.get("error") or "失败"),
                time=ts,
            )
            if item:
                emitted.append(item)
        return emitted

    def _upsert(self, url: str, **fields: Any) -> dict[str, Any] | None:
        prev = self._states.get(url, {})
        new_status = fields.get("status", prev.get("status"))
        title = fields.get("title") or prev.get("title") or url
        is_new = url not in self._states
        status_changed = not is_new and prev.get("status") != new_status
        title_improved = (
            not is_new
            and title != prev.get("title")
            and len(title) > len(str(prev.get("title") or ""))
        )
        if not is_new and not status_changed and not title_improved:
            return None

        index = prev.get("index") if not is_new else len(self._states) + 1
        item = {
            "url": url,
            "title": title,
            "status": new_status,
            "index": index,
            "page": fields.get("page", prev.get("page")),
            "total": fields.get("total", prev.get("total")),
            "message": fields.get("message") or prev.get("message") or "",
            "time": fields.get("time") or prev.get("time") or "",
        }
        item = {k: v for k, v in item.items() if v is not None and v != ""}
        self._states[url] = item
        return item
