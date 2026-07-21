"""中国政府采购网 (ccgp.gov.cn) 适配器。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

# 中央公告列表页（招标公告）
NATIONAL_LIST_URL = "http://www.ccgp.gov.cn/cggg/zygg/gkzb/index.html"

# 省级站常见列表路径（按优先级尝试）
DEFAULT_LIST_PATHS = [
    "/xxgg/gkzb/index.html",
    "/xxgg/index.html",
    "/zfcg/index.html",
    "/",
]

LIST_SELECTORS = "ul li a, table tr a, .vT-srch-result-list a, .list-item a, .news-list a"
ITEM_SELECTORS = "ul.vT-srch-result-list-bid li, ul li, table tr, .list-item, .news-list li"

DETAIL_CONTENT_SELECTOR = (
    ".article_content, #content1, .article_p, "
    ".vF_detail_content, .vF_deail_maincontent, #detail_content, .article-content"
)
DETAIL_TITLE_SELECTOR = (
    ".article_head h3, .article_head span[id*=\"title\"], "
    ".vF_detail_header h2, .vF_detail_main h2, h2.tc, #detail_title, h1"
)

_DETAIL_JS = """() => {
    const titleEl = document.querySelector('%s');
    const contentEl = document.querySelector('%s');
    const title = titleEl ? titleEl.innerText.trim() : '';
    const contentHtml = contentEl ? contentEl.innerHTML : '';
    const contentText = contentEl ? contentEl.innerText.trim() : '';
    const attachments = [];
    const container = contentEl || document.body;
    for (const a of container.querySelectorAll('a[href]')) {
        const href = a.href;
        const text = (a.innerText || '').trim();
        if (/\\.(pdf|doc|docx|xls|xlsx|zip|rar)(\\?|$)/i.test(href)
            || /附件|下载/.test(text)) {
            attachments.push({ url: href, name: text || href.split('/').pop() });
        }
    }
    return { title, contentHtml, contentText, attachments };
}""" % (
    DETAIL_TITLE_SELECTOR.replace("'", "\\'"),
    DETAIL_CONTENT_SELECTOR.replace("'", "\\'"),
)


class CcgpBaseAdapter(BaseAdapter):
    """CCGP 系列站点通用列表解析（中央 + 省级结构类似）。"""

    @staticmethod
    def _extract_meta_field(text: str, labels: list[str]) -> Optional[str]:
        for label in labels:
            m = re.search(
                rf"{re.escape(label)}[：:\s]*([^\n\r，,；;]+)",
                text,
            )
            if m:
                value = m.group(1).strip()
                if value and len(value) < 200:
                    return value
        return None

    async def fetch_detail(self, notice: BidNotice) -> BidNotice:
        async with self.browser.new_page() as page:
            response = await page.goto(notice.url, wait_until="domcontentloaded", timeout=60000)
            if response and response.status >= 400:
                logger.warning("详情页 HTTP %s: %s", response.status, notice.url)
                return notice

            try:
                await page.wait_for_selector(DETAIL_CONTENT_SELECTOR, timeout=15000)
            except Exception:
                logger.debug("详情正文选择器未匹配: %s", notice.url)

            detail = await page.evaluate(_DETAIL_JS)
            if detail.get("title"):
                notice.title = detail["title"]
            if detail.get("contentText"):
                notice.content_text = detail["contentText"]
            if detail.get("contentHtml"):
                notice.content_html = detail["contentHtml"]
            if detail.get("attachments"):
                notice.attachments = detail["attachments"]

        return notice

    def get_list_urls(self) -> list[str]:
        """子类可覆盖，返回待尝试的列表页 URL。"""
        base = self.base_url.rstrip("/")
        return [urljoin(base + "/", p.lstrip("/")) for p in DEFAULT_LIST_PATHS]

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        for list_url in self.get_list_urls():
            notices = await self._scrape_list_page(list_url, max_items)
            if notices:
                logger.info("站点 %s 从 %s 解析到 %d 条", self.site_id, list_url, len(notices))
                return notices
        logger.warning("站点 %s 所有列表页均未解析到公告", self.site_id)
        return []

    async def _scrape_list_page(self, list_url: str, max_items: int) -> list[BidNotice]:
        notices: list[BidNotice] = []
        region = self.config.get("region")

        async with self.browser.new_page() as page:
            response = await page.goto(
                list_url, wait_until="domcontentloaded", timeout=60000
            )
            if response and response.status >= 400:
                return []

            try:
                await page.wait_for_selector(LIST_SELECTORS, timeout=15000)
            except Exception:
                logger.debug("列表选择器未匹配: %s", list_url)

            items = await page.query_selector_all(ITEM_SELECTORS)
            for item in items:
                if len(notices) >= max_items:
                    break
                notice = await self._parse_item(item, page, region)
                if notice:
                    notices.append(notice)

            if not notices:
                links = await page.query_selector_all("a[href]")
                for link in links:
                    if len(notices) >= max_items:
                        break
                    href = await link.get_attribute("href")
                    title = (await link.inner_text()).strip()
                    if not href or not title or len(title) < 8:
                        continue
                    if not self._is_notice_link(href, title):
                        continue
                    full_url = urljoin(page.url, href)
                    notices.append(
                        BidNotice(
                            title=title,
                            url=full_url,
                            source_site_id=self.site_id,
                            source_site_name=self.site_name,
                            source_url=list_url,
                            region=region,
                            category=NoticeCategory.TENDER,
                        )
                    )

        return notices

    async def _parse_item(self, item, page, region: Optional[str]) -> Optional[BidNotice]:
        link = await item.query_selector("a")
        if not link:
            return None
        href = await link.get_attribute("href")
        title = (await link.inner_text()).strip()
        if not href or not title or len(title) < 5:
            return None
        if not self._is_notice_link(href, title):
            return None

        full_url = urljoin(page.url, href)
        publish_date = await self._extract_date(item)
        return BidNotice(
            title=title,
            url=full_url,
            source_site_id=self.site_id,
            source_site_name=self.site_name,
            source_url=page.url,
            publish_date=publish_date,
            region=region,
            category=NoticeCategory.TENDER,
        )

    def _is_notice_link(self, href: str, title: str) -> bool:
        if href.startswith("javascript:") or href == "#":
            return False
        skip_words = ("首页", "联系我们", "意见反馈", "English", "返回", "登录", "注册")
        if any(w in title for w in skip_words):
            return False
        if ".html" in href or "cggg" in href or "detail" in href.lower():
            return True
        if "announcement" in href.lower() or "notice" in href.lower():
            return True
        if len(title) >= 10:
            return True
        return False

    async def _extract_date(self, item) -> Optional[datetime]:
        text = await item.inner_text()
        m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None


class CcgpNationalAdapter(CcgpBaseAdapter):
    """中国政府采购网 — 中央公开招标公告列表。"""

    def get_list_urls(self) -> list[str]:
        return [NATIONAL_LIST_URL, "https://www.ccgp.gov.cn/"]
