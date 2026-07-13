"""省级公共资源交易平台适配器。"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

from src.adapters.ggzy import GgzyBaseAdapter
from src.core.models import BidNotice

logger = logging.getLogger(__name__)

# 各省站列表页（路径差异大）
SITE_LIST_URLS: dict[str, list[str]] = {
    "ggzy_北京市": [
        "https://ggzyfw.beijing.gov.cn/jyxxggjtbyqs/index.html",
    ],
    "ggzy_上海市": [
        "https://www.shggzy.com/",
    ],
    "ggzy_广东省": [
        "https://ygp.gdzwfw.gov.cn/ggzy-portal/index.html",
    ],
    "ggzy_江苏省": [
        "http://jsggzy.jszwfw.gov.cn/",
    ],
    "ggzy_浙江省": [
        "https://ggzy.zj.gov.cn/jyxxgk/list.html",
        "https://ggzy.zj.gov.cn/",
    ],
    "ggzy_四川省": [
        "http://ggzyjy.sc.gov.cn/",
        "https://ggzyjy.sc.gov.cn/",
    ],
    "ggzy_湖北省": [
        "https://www.hbggzyfwpt.cn/",
    ],
    "ggzy_山东省": [
        "http://ggzyjy.shandong.gov.cn/queryContent-jyxxgk.jspx?channelId=78",
    ],
    "ggzy_河南省": [
        "http://hnsggzyjy.henan.gov.cn/jyxx/transaction_notice.html",
        "http://hnsggzyjy.henan.gov.cn/",
    ],
    "ggzy_安徽省": [
        "https://ggzy.ah.gov.cn/jsgc/list",
        "https://ggzy.ah.gov.cn/",
    ],
}

_BEIJING_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="/jyxxggjtbyqs/"]')) {
        const title = (a.getAttribute('title') || a.innerText || '').trim().replace(/\\s+/g, ' ');
        if (!title || title.length < 8) continue;
        const parent = a.closest('li, tr, div');
        let dateText = '';
        if (parent) {
            const m = parent.innerText.match(/(\\d{4}-\\d{2}-\\d{2})/);
            dateText = m ? m[1] : '';
        }
        out.push({ title, href: a.href, dateText });
    }
    return out;
}"""

_SHANGHAI_JS = """() => {
    const out = [];
    for (const el of document.querySelectorAll('[onclick*="window.open"]')) {
        const onclick = el.getAttribute('onclick') || '';
        const m = onclick.match(/window\\.open\\('([^']+)'\\)/);
        const path = m ? m[1] : '';
        if (!path.startsWith('/jyxx')) continue;
        const titleEl = el.querySelector('.notice-title p, .notice-title, .notice-content p');
        let title = titleEl ? titleEl.innerText.trim() : el.innerText.trim();
        title = title.replace(/^\\d{4}[-/]\\d{2}[-/]\\d{2}\\s*/, '').replace(/\\s+/g, ' ');
        if (!title || title.length < 8) continue;
        const date = el.getAttribute('data-dd') || '';
        out.push({ title, href: new URL(path, location.origin).href, dateText: date });
    }
    return out;
}"""

_GUANGDONG_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="noticeId"]')) {
        const title = (a.innerText || '').trim();
        if (!title || title.length < 8) continue;
        let dateText = '';
        const pub = a.href.match(/publishDate=(\\d{8})/);
        if (pub) {
            const d = pub[1];
            dateText = d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8);
        }
        out.push({ title, href: a.href, dateText });
    }
    return out;
}"""

_JYXX_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="/jyxx/"]')) {
        const title = (a.getAttribute('title') || a.innerText || '').trim().replace(/\\s+/g, ' ');
        if (!title || title.length < 8 || title.startsWith('.cls')) continue;
        const parent = a.closest('li, tr, div');
        let dateText = '';
        if (parent) {
            const m = parent.innerText.match(/(\\d{4}-\\d{2}-\\d{2})/);
            dateText = m ? m[1] : '';
        }
        out.push({ title, href: a.href, dateText });
    }
    return out;
}"""

_HUBEI_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="Detail"], a[href*="guid="]')) {
        const title = (a.innerText || '').trim();
        if (!title || title.length < 8) continue;
        out.push({ title, href: a.href, dateText: '' });
    }
    return out;
}"""

_SHANDONG_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href$=".jhtml"]')) {
        const title = (a.innerText || '').trim();
        if (!title || title.length < 12) continue;
        out.push({ title, href: a.href, dateText: '' });
    }
    return out;
}"""

_ZHEJIANG_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="/jyxxgk/"]')) {
        const title = (a.innerText || '').trim();
        if (!title || title.length < 12) continue;
        out.push({ title, href: a.href, dateText: '' });
    }
    return out;
}"""

_ANHUI_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="newDetail"], a[href*="tzggDetail"]')) {
        const title = (a.innerText || '').trim();
        if (!title || title.length < 10) continue;
        out.push({ title, href: a.href, dateText: '' });
    }
    return out;
}"""

_SITE_SCRAPERS: dict[str, tuple[str, str]] = {
    "ggzy_北京市": ("domcontentloaded", _BEIJING_JS),
    "ggzy_上海市": ("domcontentloaded", _SHANGHAI_JS),
    "ggzy_广东省": ("networkidle", _GUANGDONG_JS),
    "ggzy_江苏省": ("domcontentloaded", _JYXX_JS),
    "ggzy_浙江省": ("domcontentloaded", _ZHEJIANG_JS),
    "ggzy_四川省": ("domcontentloaded", _JYXX_JS),
    "ggzy_湖北省": ("domcontentloaded", _HUBEI_JS),
    "ggzy_山东省": ("domcontentloaded", _SHANDONG_JS),
    "ggzy_河南省": ("domcontentloaded", _JYXX_JS),
    "ggzy_安徽省": ("domcontentloaded", _ANHUI_JS),
}


class GgzyProvincialAdapter(GgzyBaseAdapter):
    """省级公共资源交易平台 — 北京/上海/广东及 Phase 4 扩展省份。"""

    def get_list_urls(self) -> list[str]:
        urls = SITE_LIST_URLS.get(self.site_id)
        if urls:
            return urls
        base = self.base_url.rstrip("/")
        return [base + "/", urljoin(base + "/", "index.html")]

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        scraper = _SITE_SCRAPERS.get(self.site_id)
        list_urls = self.get_list_urls()

        for list_url in list_urls:
            notices = await self._scrape_list_page(
                list_url,
                max_items,
                wait_until=scraper[0] if scraper else "domcontentloaded",
                evaluate_js=scraper[1] if scraper else _BEIJING_JS,
            )
            if notices:
                logger.info("站点 %s 从 %s 解析 %d 条", self.site_id, list_url, len(notices))
                return notices

        logger.warning("站点 %s 所有列表页均未解析到公告", self.site_id)
        return []

    async def _scrape_list_page(
        self,
        list_url: str,
        max_items: int,
        wait_until: str,
        evaluate_js: str,
    ) -> list[BidNotice]:
        notices: list[BidNotice] = []
        seen_urls: set[str] = set()

        async with self.browser.new_page() as page:
            response = await page.goto(list_url, wait_until=wait_until, timeout=60000)
            if response and response.status >= 400:
                logger.warning("站点 %s 列表页 HTTP %s: %s", self.site_id, response.status, list_url)
                return []

            if self.site_id == "ggzy_上海市":
                await page.wait_for_timeout(3000)
            elif self.site_id == "ggzy_广东省":
                await page.wait_for_timeout(2000)
            elif self.site_id in (
                "ggzy_江苏省",
                "ggzy_四川省",
                "ggzy_河南省",
                "ggzy_安徽省",
                "ggzy_山东省",
            ):
                await page.wait_for_timeout(5000)

            rows = await page.evaluate(evaluate_js)

            for row in rows:
                if len(notices) >= max_items:
                    break
                href = row.get("href", "").strip()
                title = row.get("title", "").strip()
                if not href or not title or href in seen_urls:
                    continue
                if href.startswith("javascript:"):
                    continue
                seen_urls.add(href)
                publish_date = self._parse_date(row.get("dateText", ""), href)
                notices.append(
                    BidNotice(
                        title=title,
                        url=href,
                        source_site_id=self.site_id,
                        source_site_name=self.site_name,
                        source_url=list_url,
                        publish_date=publish_date,
                        category=self._guess_category(title),
                    )
                )

        return notices
