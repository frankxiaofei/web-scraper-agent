"""招投标资讯网 (chinazbbid.com) 适配器。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

LIST_URLS = [
    "https://www.chinazbbid.com/",
    "https://www.chinazbbid.com/e/action/ListInfo/?classid=3",
]

LINK_SELECTOR = "a[href*='ShowInfo.php'], a[href*='/news/'], a[href*='classid=']"
NOTICE_KEYWORDS = ("招标", "采购", "中标", "公告", "成交", "竞争性", "磋商", "询价", "比选")
SKIP_TITLES = ("首页", "联系我们", "登录", "注册", "English", "返回", "更多")


class ChinazbbidAdapter(BaseAdapter):
    """招投标资讯网 — 招标采购公告列表。"""

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        for list_url in LIST_URLS:
            notices = await self._scrape_list_page(list_url, max_items)
            if notices:
                logger.info("站点 %s 从 %s 解析到 %d 条", self.site_id, list_url, len(notices))
                return notices
        logger.warning("站点 %s 所有列表页均未解析到公告", self.site_id)
        return []

    async def _scrape_list_page(self, list_url: str, max_items: int) -> list[BidNotice]:
        notices: list[BidNotice] = []
        seen: set[str] = set()

        async with self.browser.new_page() as page:
            response = await page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
            if response and response.status >= 400:
                return []

            await page.wait_for_timeout(1000)

            links = await page.query_selector_all(LINK_SELECTOR)
            if not links:
                links = await page.query_selector_all("a[href]")

            for link in links:
                if len(notices) >= max_items:
                    break
                notice = await self._parse_link(link, page, list_url, seen)
                if notice:
                    notices.append(notice)

        return notices

    async def _parse_link(self, link, page, list_url: str, seen: set[str]) -> Optional[BidNotice]:
        href = await link.get_attribute("href")
        title = (await link.inner_text()).strip()
        if not href or not title or len(title) < 8:
            return None
        if not self._is_notice_link(href, title):
            return None

        full_url = urljoin(page.url, href)
        if full_url in seen:
            return None
        seen.add(full_url)

        parent = await link.evaluate_handle("el => el.closest('li, tr, div')")
        publish_date = None
        if parent:
            try:
                text = await parent.as_element().inner_text()
                publish_date = self._extract_date(text)
            except Exception:
                pass

        return BidNotice(
            title=title,
            url=full_url,
            source_site_id=self.site_id,
            source_site_name=self.site_name,
            source_url=list_url,
            publish_date=publish_date,
            category=NoticeCategory.TENDER,
        )

    def _is_notice_link(self, href: str, title: str) -> bool:
        if href.startswith("javascript:") or href in ("#", "/"):
            return False
        if any(w in title for w in SKIP_TITLES):
            return False
        host = urlparse(urljoin(self.base_url, href)).netloc
        if host and "chinazbbid.com" not in host:
            return False
        if "ShowInfo.php" in href:
            return len(title) >= 6
        if not any(k in title for k in NOTICE_KEYWORDS):
            return False
        return True

    def _extract_date(self, text: str) -> Optional[datetime]:
        m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None
