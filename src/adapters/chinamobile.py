"""中国移动采购与招标网 (b2b.10086.cn) 适配器 — SPA + 首页 API。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

INDEX_URL = "https://b2b.10086.cn/#/index"


class ChinaMobileAdapter(BaseAdapter):
    """中国移动采购与招标网 — 首页正在招标列表（浏览器内 fetch，规避 SSL 直连问题）。"""

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        notices: list[BidNotice] = []
        seen: set[str] = set()
        api_rows: list[dict[str, Any]] = []

        async with self.browser.new_page() as page:

            async def on_response(response: Any) -> None:
                if "homePageQueryList" not in response.url or response.status != 200:
                    return
                try:
                    body = await response.json()
                    api_rows.extend(body.get("data", {}).get("content", []))
                except Exception:
                    pass

            page.on("response", on_response)

            await page.goto(INDEX_URL, wait_until="networkidle", timeout=90000)
            await page.wait_for_selector(".list-item .notice-title", timeout=30000)

            rows = api_rows if api_rows else await page.evaluate(
                """() => {
                    const out = [];
                    for (const el of document.querySelectorAll('.list-item')) {
                        const titleEl = el.querySelector('.notice-title, .tooltop-text');
                        const title = titleEl ? titleEl.innerText.trim() : el.innerText.trim();
                        let deadline = '';
                        const m = el.innerText.match(/截止时间：\\s*([\\d-: ]+)/);
                        if (m) deadline = m[1].trim();
                        if (title.length >= 10) out.push({ name: title, publishDate: deadline, id: null });
                    }
                    return out;
                }"""
            )

            for row in rows:
                if len(notices) >= max_items:
                    break
                notice = self._row_to_notice(row, seen)
                if notice:
                    notices.append(notice)

        logger.info("站点 %s 解析 %d 条", self.site_id, len(notices))
        return notices

    def _row_to_notice(self, row: dict[str, Any], seen: set[str]) -> Optional[BidNotice]:
        title = (row.get("name") or row.get("title") or "").strip()
        if not title or len(title) < 8:
            return None

        item_id = row.get("id") or row.get("publishId")
        if item_id:
            url = f"https://b2b.10086.cn/#/noticeDetail?publishId={item_id}"
        else:
            url = f"{INDEX_URL}?q={title[:80]}"

        if url in seen:
            return None
        seen.add(url)

        return BidNotice(
            title=title,
            url=url,
            source_site_id=self.site_id,
            source_site_name=self.site_name,
            source_url=INDEX_URL,
            publish_date=self._parse_date(row.get("publishDate") or row.get("tenderSaleDeadline")),
            category=self._guess_category(title),
        )

    def _parse_date(self, text: str) -> Optional[datetime]:
        if not text:
            return None
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(text))
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        return None

    def _guess_category(self, title: str) -> NoticeCategory:
        if "中标" in title or "中选" in title or "成交" in title:
            return NoticeCategory.WIN
        if "变更" in title or "重发" in title or "澄清" in title:
            return NoticeCategory.CHANGE
        return NoticeCategory.TENDER
