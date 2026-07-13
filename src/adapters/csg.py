"""中国南方电网供应链平台 (bidding.csg.cn) 适配器。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

BID_LIST_URL = "http://www.bidding.csg.cn/zbcg/index.jhtml"


class CsgBiddingAdapter(BaseAdapter):
    """南方电网供应链统一服务平台 — 标讯列表。"""

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        notices: list[BidNotice] = []
        seen: set[str] = set()

        async with self.browser.new_page() as page:
            response = await page.goto(BID_LIST_URL, wait_until="networkidle", timeout=60000)
            if response and response.status >= 400:
                logger.warning("站点 %s 列表页 HTTP %s", self.site_id, response.status)
                return []

            await page.wait_for_selector("a[href*='/zbgg/'], a[href*='/zbhxrgs/']", timeout=30000)

            rows = await page.evaluate(
                """() => {
                    const out = [];
                    for (const a of document.querySelectorAll('a[href*="/zbgg/"], a[href*="/zbhxrgs/"], a[href*="/zbgs/"]')) {
                        const title = (a.getAttribute('title') || a.innerText || '').trim().replace(/\\s+/g, ' ');
                        const href = a.href;
                        if (!title || title.length < 10 || !href) continue;
                        let dateText = '';
                        const row = a.closest('li, tr, div');
                        if (row) {
                            const m = row.innerText.match(/(\\d{4}-\\d{2}-\\d{2})/);
                            if (m) dateText = m[1];
                        }
                        out.push({ title, href, dateText });
                    }
                    return out;
                }"""
            )

            for row in rows:
                if len(notices) >= max_items:
                    break
                href = row.get("href", "").strip()
                title = row.get("title", "").strip()
                if not href or not title or href in seen:
                    continue
                seen.add(href)
                notices.append(
                    BidNotice(
                        title=title,
                        url=href,
                        source_site_id=self.site_id,
                        source_site_name=self.site_name,
                        source_url=BID_LIST_URL,
                        publish_date=self._parse_date(row.get("dateText", "")),
                        category=self._guess_category(title),
                    )
                )

        logger.info("站点 %s 从标讯列表解析 %d 条", self.site_id, len(notices))
        return notices

    def _parse_date(self, text: str) -> Optional[datetime]:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        return None

    def _guess_category(self, title: str) -> NoticeCategory:
        if "中标" in title or "成交" in title or "公示" in title:
            return NoticeCategory.WIN
        if "更正" in title or "变更" in title or "澄清" in title:
            return NoticeCategory.CHANGE
        return NoticeCategory.TENDER
