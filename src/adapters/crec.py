"""中国铁路工程鲁班商务网 (crecgec.com) 适配器 — 电子采购门户 SPA。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

PORTAL_HOME = "https://eproport.crecgec.com/#/home"
DETAIL_URL_TEMPLATE = "https://eproport.crecgec.com/#/notice/noticeView?pubId={pub_id}"


class CrecAdapter(BaseAdapter):
    """中铁鲁班商务网 — 电子采购门户首页采购公告 / 中标公示。"""

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        notices: list[BidNotice] = []
        seen: set[str] = set()
        captured: list[dict[str, Any]] = []

        async with self.browser.new_page() as page:

            async def on_response(response: Any) -> None:
                url = response.url
                if response.status != 200:
                    return
                if "project/listWithHomePage" not in url and "bid_notice/listWithHomePage" not in url:
                    return
                try:
                    body = await response.json()
                    for rec in body.get("data", {}).get("records", []):
                        captured.append(rec)
                except Exception:
                    pass

            page.on("response", on_response)

            await page.goto(PORTAL_HOME, wait_until="networkidle", timeout=90000)
            await page.wait_for_timeout(3000)

            if not captured:
                logger.warning("站点 %s 未捕获门户 API 数据", self.site_id)
                return []

            for row in captured:
                if len(notices) >= max_items:
                    break
                notice = self._row_to_notice(row, seen)
                if notice:
                    notices.append(notice)

        logger.info("站点 %s 从电子采购门户解析 %d 条", self.site_id, len(notices))
        return notices

    def _row_to_notice(self, row: dict[str, Any], seen: set[str]) -> Optional[BidNotice]:
        title = (row.get("noticeTitle") or row.get("pubTitle") or row.get("projectName") or "").strip()
        pub_id = row.get("pubId") or row.get("id")
        if not title or not pub_id:
            return None

        url = DETAIL_URL_TEMPLATE.format(pub_id=pub_id)
        if url in seen:
            return None
        seen.add(url)

        date_text = row.get("noticePublishTime") or row.get("pubTime") or ""
        return BidNotice(
            title=title,
            url=url,
            source_site_id=self.site_id,
            source_site_name=self.site_name,
            source_url=PORTAL_HOME,
            publish_date=self._parse_date(date_text),
            category=self._guess_category(title),
        )

    def _parse_date(self, text: str) -> Optional[datetime]:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(text))
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        return None

    def _guess_category(self, title: str) -> NoticeCategory:
        if "中标" in title or "成交" in title or "候选人" in title:
            return NoticeCategory.WIN
        if "变更" in title or "补遗" in title:
            return NoticeCategory.CHANGE
        return NoticeCategory.TENDER
