"""TED (Tenders Electronic Daily) EU 招标适配器（Phase 2 P2-T-010 验证）。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

TED_SEARCH_API = "https://api.ted.europa.eu/v3/notices/search"
TED_NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{notice_id}"


def _parse_ted_notice(item: dict[str, Any], site_config: dict[str, Any]) -> BidNotice | None:
    notice_id = item.get("ND") or item.get("notice-id") or item.get("id")
    title = item.get("TI") or item.get("title") or item.get("title-proc")
    if not notice_id or not title:
        return None
    pub = item.get("PD") or item.get("publication-date")
    publish_date = None
    if pub:
        try:
            publish_date = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
        except ValueError:
            publish_date = None
    buyer = item.get("buyer-name") or item.get("organisation-name-buyer")
    return BidNotice(
        title=str(title).strip(),
        url=TED_NOTICE_URL.format(notice_id=notice_id),
        source_site_id=site_config.get("id", "ted_eu"),
        source_site_name=site_config.get("name", "TED EU"),
        source_url=site_config.get("url", "https://ted.europa.eu"),
        publish_date=publish_date,
        region=item.get("CY") or item.get("country"),
        category=NoticeCategory.TENDER,
        purchaser=str(buyer).strip() if buyer else None,
        content_text=str(title),
    )


class TedAdapter(BaseAdapter):
    """欧盟 TED 公告搜索 API 适配器（HTTP，无需浏览器）。"""

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        keyword = (
            self.config.get("search_keyword")
            or self.config.get("biz_clue_search_keyword")
            or "agriculture"
        )
        query = {
            "query": f'title="{quote(str(keyword))}"',
            "page": 1,
            "limit": max(1, min(max_items, 50)),
            "scope": "ACTIVE",
            "fields": ["ND", "TI", "PD", "CY", "buyer-name"],
        }
        notices: list[BidNotice] = []
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(TED_SEARCH_API, json=query)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            logger.warning("TED API fetch failed: %s", exc)
            return notices

        for item in payload.get("results") or payload.get("notices") or []:
            notice = _parse_ted_notice(item, self.config)
            if notice:
                notices.append(notice)
            if len(notices) >= max_items:
                break
        return notices
