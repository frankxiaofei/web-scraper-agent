"""中国招标投标公共服务平台 (cebpubservice.cn) 适配器。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

# 行业监测与平台公告（bulletin 专栏需验证码，MVP 先用可访问列表页）
LIST_URLS = [
    "http://cebpubservice.cn/monitorindustry/monitorplat/index.shtml",
    "http://cebpubservice.cn/monitorindustry/observe/index.shtml",
    "http://cebpubservice.cn/",
]

NOTICE_LINK_SELECTOR = (
    "a[href*='monitorplat/'], a[href*='observe/'], a[href*='monitorindustry/']"
)


class CebpubserviceAdapter(BaseAdapter):
    """中国招标投标公共服务平台 — 行业监测与平台公告列表。"""

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
            response = await page.goto(list_url, wait_until="domcontentloaded")
            if response and response.status >= 400:
                return []

            try:
                await page.wait_for_selector(NOTICE_LINK_SELECTOR, timeout=15000)
            except Exception:
                logger.debug("列表选择器未匹配: %s", list_url)

            for link in await page.query_selector_all(NOTICE_LINK_SELECTOR):
                if len(notices) >= max_items:
                    break
                notice = await self._parse_link(link, page, list_url, seen)
                if notice:
                    notices.append(notice)

        return notices

    async def _parse_link(self, link, page, list_url: str, seen: set[str]) -> Optional[BidNotice]:
        href = await link.get_attribute("href")
        title = (await link.inner_text()).strip()
        if not href or not title or len(title) < 10:
            return None
        if not self._is_notice_link(href, title):
            return None

        full_url = urljoin(page.url, href)
        if full_url in seen:
            return None
        seen.add(full_url)

        publish_date = self._extract_date_from_url(full_url) or self._extract_date(title)
        return BidNotice(
            title=title,
            url=full_url,
            source_site_id=self.site_id,
            source_site_name=self.site_name,
            source_url=list_url,
            publish_date=publish_date,
            category=NoticeCategory.OTHER,
        )

    def _is_notice_link(self, href: str, title: str) -> bool:
        if href.startswith("javascript:") or href.endswith("index.shtml"):
            return False
        skip = ("首页", "联系我们", "登录", "注册", "专栏首页", "发布工具", "发布媒介")
        if any(w in title for w in skip):
            return False
        if re.search(r"/\d{4}/\d{2}/\d+\.shtml", href):
            return True
        if ".shtml" in href and len(title) >= 12:
            return True
        return False

    def _extract_date_from_url(self, url: str) -> Optional[datetime]:
        m = re.search(r"/(\d{4})/(\d{1,2})/", url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), 1)
            except ValueError:
                return None
        return None

    def _extract_date(self, text: str) -> Optional[datetime]:
        m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None
