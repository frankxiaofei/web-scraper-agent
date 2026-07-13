"""中国石化物资采购平台 (ec.sinopec.com) 适配器。"""

from __future__ import annotations

import logging

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

INDEX_URL = "https://ec.sinopec.com/supp/index.shtml"


class SinopecAdapter(BaseAdapter):
    """中国石化易派客 — 首页招标/采购公告滚动列表。"""

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        notices: list[BidNotice] = []
        seen: set[str] = set()

        async with self.browser.new_page() as page:
            response = await page.goto(INDEX_URL, wait_until="networkidle", timeout=60000)
            if response and response.status >= 400:
                logger.warning("站点 %s 首页 HTTP %s", self.site_id, response.status)
                return []

            await page.wait_for_selector('a[href*="bidNotice.do"]', timeout=30000)

            rows = await page.evaluate(
                """() => {
                    const out = [];
                    for (const a of document.querySelectorAll('a[href*="bidNotice.do"], a[href*="viewNotice.do"]')) {
                        const title = (a.getAttribute('title') || a.innerText || '').trim().replace(/\\s+/g, ' ');
                        const href = a.href;
                        if (!title || title.length < 8 || !href) continue;
                        if (title.includes('供应商处理') || title.includes('钓鱼攻击')) continue;
                        out.push({ title, href });
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
                if "bidNotice.do" not in href:
                    continue
                seen.add(href)
                notices.append(
                    BidNotice(
                        title=title,
                        url=href,
                        source_site_id=self.site_id,
                        source_site_name=self.site_name,
                        source_url=INDEX_URL,
                        category=self._guess_category(title),
                    )
                )

        logger.info("站点 %s 从首页公告解析 %d 条", self.site_id, len(notices))
        return notices

    def _guess_category(self, title: str) -> NoticeCategory:
        if "中标" in title or "成交" in title or "评标结果" in title or "结果公告" in title:
            return NoticeCategory.WIN
        if "变更" in title or "更正" in title or "重招" in title:
            return NoticeCategory.CHANGE
        return NoticeCategory.TENDER
