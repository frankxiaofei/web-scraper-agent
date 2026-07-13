"""省级政府采购网 (ccgp-*.gov.cn) 适配器。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

from src.adapters.ccgp import CcgpBaseAdapter, DEFAULT_LIST_PATHS
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

# 部分省级站需优先尝试备用域名（旧 http 域名可能超时或已跳转）
SITE_BASE_URLS: dict[str, list[str]] = {
    "ccgp_广东省": [
        "https://gdgpo.czt.gd.gov.cn",
        "https://www.ccgp-guangdong.gov.cn",
    ],
    "ccgp_上海市": [
        "http://www.zfcg.sh.gov.cn",
        "https://www.zfcg.sh.gov.cn",
    ],
}

# 各省站列表页路径差异较大，按站单独配置
SITE_LIST_PATHS: dict[str, list[str]] = {
    "ccgp_北京市": [
        "/",
        "/zfcg/index.html",
        "/xxgg/gkzb/index.html",
    ],
    "ccgp_上海市": [
        "/site/category?parentId=137027&childrenCode=ZcyAnnouncement",
        "/",
    ],
    "ccgp_广东省": [
        "/pub/category/list.html?parentId=66485&childrenCode=ZcyAnnouncement",
        "/xxgg/gkzb/index.html",
    ],
    "ccgp_江苏省": ["/"],
    "ccgp_浙江省": ["/"],
    "ccgp_湖北省": ["/"],
    "ccgp_河南省": ["https://zfcg.henan.gov.cn/"],
    "ccgp_四川省": ["http://www.ccgp-sichuan.gov.cn/"],
    "ccgp_云南省": [
        "http://www.ccgp-yunnan.gov.cn/page/procurement/procurementList.html",
    ],
}

# 云南站 Bootgrid REST API
_YUNNAN_API = "/api/procurement/Procurement.gghtMoreList.svc"
_YUNNAN_DETAIL = "/showBulletinInfo.html?bulletin_id={bulletin_id}"
_YUNNAN_BASE = "http://www.ccgp-yunnan.gov.cn"

# 四川省 REST API（gpcms 一体化平台）
_SICHUAN_API = (
    "http://www.ccgp-sichuan.gov.cn/gpcms/rest/web/v2/info/selectInfoMoreChannel"
    "?siteId=94c965cc-c55d-4f92-8469-d5875c68bd04"
    "&channel=c5bff13f-21ca-4dac-b158-cb40accd3035"
)
_SICHUAN_DETAIL = "http://www.ccgp-sichuan.gov.cn/maincms-web/article?type=notice&id={id}"

# 广东省 REST API（gpcms 智慧云平台，旧 http 域名已不可用）
_GUANGDONG_API = (
    "https://gdgpo.czt.gd.gov.cn/gpcms/rest/web/v2/info/selectInfoMoreChannel"
    "?siteId=cd64e06a-21a7-4620-aebc-0576bab7e07a"
    "&channel=3b49b9ba-48b6-4220-9e8b-eb89f41e9d66"
    "&noticeType=201022,201023,201111,00107D&regionCode=440001"
)
_GUANGDONG_DETAIL = "https://gdgpo.czt.gd.gov.cn/maincms-web/article?type=notice&id={id}"
_GUANGDONG_LIST_URL = "https://www.ccgp-guangdong.gov.cn/"

# 上海 REST API（旧 ccgp-shanghai 域名已跳转登录页）
_SHANGHAI_CATEGORY_API = "http://www.zfcg.sh.gov.cn/portal/category"
_SHANGHAI_CATEGORY_CODES = ("ZcyAnnouncement", "77-379845")
_SHANGHAI_DETAIL = "http://www.zfcg.sh.gov.cn/site/detail?articleId={article_id}"
_SHANGHAI_LIST_URL = "http://www.zfcg.sh.gov.cn/"

_JIANGSU_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="js_cggg/details"]')) {
        const title = (a.innerText || '').trim();
        if (!title || title.length < 8) continue;
        let dateText = '';
        const parent = a.closest('li, tr, div');
        if (parent) {
            const m = parent.innerText.match(/(\\d{4}-\\d{2}-\\d{2})/);
            dateText = m ? m[1] : '';
        }
        out.push({ title, href: a.href, dateText });
    }
    return out;
}"""

_ZHEJIANG_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="site/detail"]')) {
        let title = (a.innerText || '').trim()
            .replace(/^(普通|紧急|重要)\\s*/, '')
            .replace(/\\s+/g, ' ');
        if (!title || title.length < 10) continue;
        out.push({ title, href: a.href, dateText: '' });
    }
    return out;
}"""

_HUBEI_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="/news/"]')) {
        const title = (a.innerText || '').trim();
        if (!title || title.length < 10) continue;
        out.push({ title, href: a.href, dateText: '' });
    }
    return out;
}"""

_HENAN_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="infoId"]')) {
        const title = (a.innerText || '').trim();
        if (!title || title.length < 10) continue;
        out.push({ title, href: a.href, dateText: '' });
    }
    return out;
}"""

# 云南省政府采购网（bootgrid）：在浏览器中提取已渲染的表格数据
_YUNNAN_JS = """() => {
    const out = [];
    const rows = document.querySelectorAll('#bulletinlistid tbody tr');
    for (let i = 0; i < rows.length; i++) {
        const a = rows[i].querySelector('a.show');
        if (!a) continue;
        const title = (a.innerText || '').trim();
        if (!title || title.length < 8) continue;
        const cells = rows[i].querySelectorAll('td');
        const dateText = cells.length >= 4 ? (cells[3].textContent || '').trim() : '';
        const bulletin_id = a.getAttribute('data-bulletin_id');
        const href = bulletin_id ? '/ggmxinfo.html?bulletinid=' + bulletin_id : '';
        out.push({ title, href, dateText });
    }
    return out;
}"""

_SITE_SCRAPERS: dict[str, tuple[str, str]] = {
    "ccgp_江苏省": ("domcontentloaded", _JIANGSU_JS),
    "ccgp_浙江省": ("domcontentloaded", _ZHEJIANG_JS),
    "ccgp_湖北省": ("domcontentloaded", _HUBEI_JS),
    "ccgp_河南省": ("domcontentloaded", _HENAN_JS),
    "ccgp_云南省": ("networkidle", _YUNNAN_JS),
}


class CcgpProvincialAdapter(CcgpBaseAdapter):
    """省级政府采购网 — 结构与中央站类似，列表页路径因省而异。"""

    def get_list_urls(self) -> list[str]:
        paths = SITE_LIST_PATHS.get(self.site_id)
        if paths:
            bases = SITE_BASE_URLS.get(self.site_id, [self.base_url.rstrip("/")])
            urls: list[str] = []
            seen: set[str] = set()
            for base in bases:
                for p in paths:
                    url = p if p.startswith("http") else urljoin(base + "/", p.lstrip("/"))
                    if url not in seen:
                        seen.add(url)
                        urls.append(url)
            return urls
        base = self.base_url.rstrip("/")
        return [urljoin(base + "/", p.lstrip("/")) for p in DEFAULT_LIST_PATHS]

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        if self.site_id == "ccgp_四川省":
            return await self._fetch_sichuan_api(max_items)
        if self.site_id == "ccgp_广东省":
            return await self._fetch_guangdong_api(max_items)
        if self.site_id == "ccgp_上海市":
            return await self._fetch_shanghai_api(max_items)
        if self.site_id == "ccgp_云南省":
            # 优先使用 Bootgrid REST API，绕过浏览器翻页滑块验证码
            return await self._fetch_yunnan_api(max_items)

        scraper = _SITE_SCRAPERS.get(self.site_id)
        if scraper:
            return await self._fetch_with_js_scraper(scraper[0], scraper[1], max_items)

        return await super().fetch_list(max_items)

    async def _fetch_sichuan_api(self, max_items: int) -> list[BidNotice]:
        notices: list[BidNotice] = []
        list_url = self.get_list_urls()[0]

        async with self.browser.new_page() as page:
            for page_no in range(1, 4):
                if len(notices) >= max_items:
                    break
                api_url = f"{_SICHUAN_API}&pageNo={page_no}&pageSize={max_items}"
                response = await page.request.get(api_url)
                if response.status >= 400:
                    logger.warning("站点 %s API HTTP %s", self.site_id, response.status)
                    break
                try:
                    payload = json.loads(await response.text())
                except json.JSONDecodeError:
                    break
                rows = payload.get("data", {}).get("rows", [])
                if not rows:
                    break
                for row in rows:
                    if len(notices) >= max_items:
                        break
                    title = (row.get("title") or "").strip()
                    row_id = row.get("id")
                    if not title or not row_id:
                        continue
                    publish_date = self._parse_sichuan_date(row.get("noticeTime") or row.get("publishTime"))
                    notices.append(
                        BidNotice(
                            title=title,
                            url=_SICHUAN_DETAIL.format(id=row_id),
                            source_site_id=self.site_id,
                            source_site_name=self.site_name,
                            source_url=list_url,
                            publish_date=publish_date,
                            region=self.config.get("region"),
                            category=NoticeCategory.TENDER,
                        )
                    )

        if notices:
            logger.info("站点 %s 从 API 解析 %d 条", self.site_id, len(notices))
        else:
            logger.warning("站点 %s API 未解析到公告", self.site_id)
        return notices

    async def _fetch_guangdong_api(self, max_items: int) -> list[BidNotice]:
        notices: list[BidNotice] = []

        async with self.browser.new_page() as page:
            for page_no in range(1, 4):
                if len(notices) >= max_items:
                    break
                api_url = f"{_GUANGDONG_API}&currPage={page_no}&pageSize={max_items}"
                response = await page.request.get(api_url)
                if response.status >= 400:
                    logger.warning("站点 %s API HTTP %s", self.site_id, response.status)
                    break
                try:
                    payload = json.loads(await response.text())
                except json.JSONDecodeError:
                    break
                rows = payload.get("data", {}).get("rows", [])
                if not rows:
                    break
                for row in rows:
                    if len(notices) >= max_items:
                        break
                    title = (row.get("title") or "").strip()
                    row_id = row.get("id")
                    if not title or not row_id:
                        continue
                    publish_date = self._parse_sichuan_date(
                        row.get("noticeTime") or row.get("publishTime")
                    )
                    notices.append(
                        BidNotice(
                            title=title,
                            url=_GUANGDONG_DETAIL.format(id=row_id),
                            source_site_id=self.site_id,
                            source_site_name=self.site_name,
                            source_url=_GUANGDONG_LIST_URL,
                            publish_date=publish_date,
                            region=self.config.get("region"),
                            category=NoticeCategory.TENDER,
                        )
                    )

        if notices:
            logger.info("站点 %s 从 API 解析 %d 条", self.site_id, len(notices))
        else:
            logger.warning("站点 %s API 未解析到公告", self.site_id)
        return notices

    async def _fetch_shanghai_api(self, max_items: int) -> list[BidNotice]:
        notices: list[BidNotice] = []
        seen_ids: set[str] = set()

        async with self.browser.new_page() as page:
            for category_code in _SHANGHAI_CATEGORY_CODES:
                if len(notices) >= max_items:
                    break
                for page_no in range(1, 4):
                    if len(notices) >= max_items:
                        break
                    payload = {
                        "pageNo": page_no,
                        "pageSize": max_items,
                        "categoryCode": category_code,
                    }
                    response = await page.request.post(
                        _SHANGHAI_CATEGORY_API,
                        data=json.dumps(payload),
                        headers={"Content-Type": "application/json"},
                    )
                    if response.status >= 400:
                        logger.warning("站点 %s API HTTP %s", self.site_id, response.status)
                        break
                    try:
                        body = json.loads(await response.text())
                    except json.JSONDecodeError:
                        break
                    rows = body.get("result", {}).get("data", {}).get("data", [])
                    if not rows:
                        break
                    for row in rows:
                        if len(notices) >= max_items:
                            break
                        title = (row.get("title") or "").strip()
                        article_id = row.get("articleId")
                        if not title or not article_id or article_id in seen_ids:
                            continue
                        seen_ids.add(article_id)
                        publish_date = self._parse_epoch_ms(row.get("publishDate"))
                        notices.append(
                            BidNotice(
                                title=title,
                                url=_SHANGHAI_DETAIL.format(article_id=article_id),
                                source_site_id=self.site_id,
                                source_site_name=self.site_name,
                                source_url=_SHANGHAI_LIST_URL,
                                publish_date=publish_date,
                                region=self.config.get("region"),
                                category=NoticeCategory.TENDER,
                            )
                        )

        if notices:
            logger.info("站点 %s 从 API 解析 %d 条", self.site_id, len(notices))
        else:
            logger.warning("站点 %s API 未解析到公告", self.site_id)
        return notices

    async def _fetch_yunnan_api(self, max_items: int = 20) -> list[BidNotice]:
        """云南省政府采购网 — Bootgrid AJAX REST API。"""
        notices: list[BidNotice] = []
        list_url = self.get_list_urls()[0]

        async with self.browser.new_page() as page:
            for page_no in range(1, 11):
                if len(notices) >= max_items:
                    break
                api_url = urljoin(_YUNNAN_BASE, _YUNNAN_API)
                params = {
                    "current": page_no,
                    "rowCount": min(max_items, 100),
                    "query_type": 1,  # 1 = 招标/预审/谈判/磋商/询价公告
                }
                response = await page.request.post(
                    api_url,
                    form=params,
                )
                if response.status >= 400:
                    logger.warning("站点 %s API HTTP %s", self.site_id, response.status)
                    break
                try:
                    payload = json.loads(await response.text())
                except json.JSONDecodeError:
                    break
                rows = payload.get("rows", [])
                if not rows:
                    break
                for row in rows:
                    if len(notices) >= max_items:
                        break
                    title = (row.get("bulletintitle") or "").strip()
                    bulletin_id = row.get("bulletin_id")
                    if not title or not bulletin_id:
                        continue
                    # 解析日期
                    publish_date = None
                    finish_day = row.get("finishday", "").strip()
                    if finish_day and len(finish_day) >= 10:
                        import re
                        m = re.search(r"(\d{4}-\d{2}-\d{2})", finish_day)
                        if m:
                            from datetime import datetime
                            try:
                                publish_date = datetime.strptime(m.group(1), "%Y-%m-%d")
                            except ValueError:
                                pass
                    detail_url = urljoin(_YUNNAN_BASE, _YUNNAN_DETAIL.format(bulletin_id=bulletin_id))
                    notices.append(
                        BidNotice(
                            title=title,
                            url=detail_url,
                            source_site_id=self.site_id,
                            source_site_name=self.site_name,
                            source_url=list_url,
                            publish_date=publish_date,
                            region=self.config.get("region"),
                            category=NoticeCategory.TENDER,
                        )
                    )

        if notices:
            logger.info("站点 %s 从 API 解析 %d 条", self.site_id, len(notices))
        else:
            logger.warning("站点 %s API 未解析到公告", self.site_id)
        return notices

    async def _fetch_with_js_scraper(
        self,
        wait_until: str,
        evaluate_js: str,
        max_items: int,
    ) -> list[BidNotice]:
        notices: list[BidNotice] = []
        seen_urls: set[str] = set()

        for list_url in self.get_list_urls():
            async with self.browser.new_page() as page:
                response = await page.goto(list_url, wait_until=wait_until, timeout=60000)
                if response and response.status >= 400:
                    logger.warning("站点 %s 列表页 HTTP %s: %s", self.site_id, response.status, list_url)
                    continue
                if self.site_id in ("ccgp_浙江省", "ccgp_河南省"):
                    await page.wait_for_timeout(5000)

                rows = await page.evaluate(evaluate_js)
                for row in rows:
                    if len(notices) >= max_items:
                        break
                    href = row.get("href", "").strip()
                    title = row.get("title", "").strip()
                    if not href or not title or href in seen_urls:
                        continue
                    seen_urls.add(href)
                    publish_date = self._parse_date_text(row.get("dateText", ""))
                    notices.append(
                        BidNotice(
                            title=title,
                            url=href,
                            source_site_id=self.site_id,
                            source_site_name=self.site_name,
                            source_url=list_url,
                            publish_date=publish_date,
                            region=self.config.get("region"),
                            category=NoticeCategory.TENDER,
                        )
                    )

            if notices:
                logger.info("站点 %s 从 %s 解析 %d 条", self.site_id, list_url, len(notices))
                return notices

        logger.warning("站点 %s 所有列表页均未解析到公告", self.site_id)
        return []

    def _parse_epoch_ms(self, value: Optional[int]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromtimestamp(int(value) / 1000)
        except (ValueError, OSError, OverflowError):
            return None

    def _parse_sichuan_date(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        import re

        value = value.strip()
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None

    def _parse_date_text(self, date_text: str) -> Optional[datetime]:
        if not date_text:
            return None
        import re

        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None
