"""国家电网 ECP (ecp.sgcc.com.cn) 适配器 — SPA 招标采购列表。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

PORTAL_BASE = "https://ecp.sgcc.com.cn/ecp2.0/portal"
NOTE_LIST_API = "https://ecp.sgcc.com.cn/ecp2.0/ecpwcmcore//index/noteList"

# 招标公告及投标邀请书（list-spe 路由末段 = firstPageMenuId）
TENDER_MENU_ID = "2018032700291334"
LIST_PAGE_URL = f"{PORTAL_BASE}/#/list/list-spe/2018032600289606_1_{TENDER_MENU_ID}"


class SgccEcpAdapter(BaseAdapter):
    """国网电子商务平台 — 通过 noteList API 抓取招标公告。"""

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        notices: list[BidNotice] = []
        seen: set[str] = set()

        async with self.browser.new_page() as page:
            await page.goto(LIST_PAGE_URL, wait_until="networkidle", timeout=90000)
            await page.wait_for_selector(".list .cur, tr.cur", timeout=30000)

            rows = await page.evaluate(
                """async (payload) => {
                    const resp = await fetch(payload.url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload.body),
                    });
                    const data = await resp.json();
                    return data?.resultValue?.noteList || [];
                }""",
                {
                    "url": NOTE_LIST_API,
                    "body": {
                        "index": 1,
                        "size": max_items,
                        "firstPageMenuId": TENDER_MENU_ID,
                        "purOrgStatus": "",
                        "purOrgCode": "",
                        "purType": "",
                        "noticeType": "",
                        "orgId": "",
                        "key": "",
                        "orgName": "",
                    },
                },
            )

            for row in rows:
                if len(notices) >= max_items:
                    break
                notice = self._row_to_notice(row, seen)
                if notice:
                    notices.append(notice)

        logger.info("站点 %s 从 noteList 解析 %d 条", self.site_id, len(notices))
        return notices

    def _row_to_notice(self, row: dict[str, Any], seen: set[str]) -> Optional[BidNotice]:
        title = (row.get("title") or "").strip()
        notice_id = row.get("noticeId")
        doctype = (row.get("doctype") or "doci-bid").strip()
        if not title or not notice_id:
            return None

        org = (row.get("publishOrgName") or "").strip()
        if org and not title.startswith("【"):
            title = f"【{org}】{title}"

        url = f"{PORTAL_BASE}/#/doc/{doctype}/{notice_id}_{TENDER_MENU_ID}"
        if url in seen:
            return None
        seen.add(url)

        return BidNotice(
            title=title,
            url=url,
            source_site_id=self.site_id,
            source_site_name=self.site_name,
            source_url=LIST_PAGE_URL,
            publish_date=self._parse_date(row.get("noticePublishTime", "")),
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
        if "中标" in title or "成交" in title:
            return NoticeCategory.WIN
        if "更正" in title or "变更" in title:
            return NoticeCategory.CHANGE
        return NoticeCategory.TENDER
