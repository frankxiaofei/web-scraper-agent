"""中央政府采购网 (zycg.gov.cn) 适配器。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

# 采购公告列表页（无 L2 规则时的回退）
LIST_URLS = [
    "https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/cggg/index.html",
    "https://www.zycg.gov.cn/",
]

LINK_SELECTORS = "a[href*='ggxx/info/'], a[href*='tzgg/info/']"


class ZycgAdapter(BaseAdapter):
    """中央政府采购网 — 采购/招标公告列表。"""

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

        from src.core.crawl_run import emit_crawl_event

        emit_crawl_event("navigate", "打开列表页", data={"url": list_url})

        async with self.browser.new_page() as page:
            response = await page.goto(list_url, wait_until="domcontentloaded")
            if response and response.status >= 400:
                return []

            try:
                await page.wait_for_selector(LINK_SELECTORS, timeout=15000)
            except Exception:
                logger.debug("列表选择器未匹配: %s", list_url)

            links = await page.query_selector_all(LINK_SELECTORS)
            for link in links:
                if len(notices) >= max_items:
                    break
                notice = await self._parse_link(link, page, list_url, seen)
                if notice:
                    notices.append(notice)

            if not notices:
                for link in await page.query_selector_all("a[href]"):
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
        if href.startswith("javascript:") or href == "#":
            return False
        skip = ("首页", "联系我们", "登录", "注册", "English", "返回")
        if any(w in title for w in skip):
            return False
        if "/ggxx/info/" in href or "/tzgg/info/" in href:
            return True
        if any(k in title for k in ("招标", "采购", "中标", "更正", "公告", "成交")):
            return "/info/" in href or ".html" in href
        return False

    def _extract_date(self, text: str) -> Optional[datetime]:
        m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None
