"""SAM.gov 美国联邦招标适配器（Phase 3 P3-T-003）。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

SAM_API = "https://api.sam.gov/opportunities/v2/search"


class SamGovAdapter(BaseAdapter):
    """SAM.gov Opportunities API（需 SAM_API_KEY 环境变量）。"""

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        from src.core.config import get_settings

        api_key = get_settings().sam_api_key
        if not api_key:
            logger.info("SAM.gov adapter skipped: SAM_API_KEY not configured")
            return []

        keyword = (
            self.config.get("search_keyword")
            or self.config.get("biz_clue_search_keyword")
            or "agriculture"
        )
        params = {
            "api_key": api_key,
            "q": keyword,
            "limit": max(1, min(max_items, 25)),
            "postedFrom": datetime.now().strftime("%m/%d/%Y"),
        }
        notices: list[BidNotice] = []
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(SAM_API, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            logger.warning("SAM.gov API fetch failed: %s", exc)
            return notices

        for item in payload.get("opportunitiesData") or []:
            notice = _parse_sam_notice(item, self.config)
            if notice:
                notices.append(notice)
            if len(notices) >= max_items:
                break
        return notices


def _parse_sam_notice(item: dict[str, Any], site_config: dict[str, Any]) -> BidNotice | None:
    title = item.get("title")
    notice_id = item.get("noticeId") or item.get("solicitationNumber")
    if not title or not notice_id:
        return None
    url = item.get("uiLink") or f"https://sam.gov/opp/{notice_id}/view"
    pub = item.get("postedDate")
    publish_date = None
    if pub:
        try:
            publish_date = datetime.strptime(str(pub)[:10], "%Y-%m-%d")
        except ValueError:
            publish_date = None
    return BidNotice(
        title=str(title).strip(),
        url=str(url),
        source_site_id=site_config.get("id", "sam_gov"),
        source_site_name=site_config.get("name", "SAM.gov"),
        source_url=site_config.get("url", "https://sam.gov"),
        publish_date=publish_date,
        region="US",
        category=NoticeCategory.TENDER,
        purchaser=item.get("fullParentPathName") or item.get("department"),
        content_text=str(title),
    )
