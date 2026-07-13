"""全国公共资源交易平台 (ggzy.gov.cn) 适配器。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory

logger = logging.getLogger(__name__)

LIST_URLS = [
    "https://www.ggzy.gov.cn/deal/dealList.html",
    "https://www.ggzy.gov.cn/",
]

DEAL_LINK_SELECTOR = "a[href*='/information/deal/html/']"

DETAIL_CONTENT_SELECTOR = (
    ".detail_content, .detailContent, .article-content, .content, #detail_content"
)
DETAIL_TITLE_SELECTOR = (
    ".detail_title, .detailTitle, .article-title, h1.title, .title h1, h1"
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


class GgzyBaseAdapter(BaseAdapter):
    """GGZY 系列站点通用日期与分类解析。"""

    def _parse_date(self, date_text: str, href: str) -> Optional[datetime]:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", date_text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        m = re.search(r"/(\d{4})(\d{2})(\d{2})/", href)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        m = re.search(r"publishDate=(\d{4})(\d{2})(\d{2})", href)
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

    async def _extract_from_a_page(self, notice: BidNotice, html: str) -> bool:
        """从 /a/ 路径页面 HTML 中提取详情（标题 + 当前 tab 内容）。
        返回 True 表示已成功提取内容。
        """
        # 提取标题
        m_title = re.search(r'<h4[^>]*class="h4_o"[^>]*>(.*?)</h4>', html, re.DOTALL)
        if m_title:
            notice.title = m_title.group(1).strip()

        # 提取当前选中 tab 的内容（li_toggle 的父 div 下的 fully_list）
        # 页面有多个 tab（采购/资审公告、中标公告、采购合同、更正事项），只取选中 tab 下的列表
        parts = []
        # 找 li_toggle 的前一个 sibling div（fully_toggle_cont）下的 fully_list
        # 更直接：提取所有 fully_list 然后取第一个有内容的
        lists = re.findall(
            r'<div[^>]*class="fully_toggle_cont"[^>]*>.*?<ul[^>]*class="fully_list"[^>]*>(.*?)</ul>',
            html, re.DOTALL
        )
        for lst in lists:
            items = re.findall(r'<li>(.*?)</li>', lst, re.DOTALL)
            for item_text in items:
                text = re.sub(r'<[^>]+>', '', item_text).strip()
                if text and text != '暂无数据':
                    parts.append(text)

        if parts:
            combined = '\n'.join(parts)
            notice.content_text = combined
            # 构建简单 HTML
            notice.content_html = '<div class="ggzy_detail">' + ''.join(
                f'<p>{p}</p>' for p in parts
            ) + '</div>'
            logger.info("ggzy /a/ 详情提取成功: %s (内容 %d 字符, %d 条目)",
                        notice.url, len(combined), len(parts))

        # 提取附件（从页面所有 a[href]）
        for a_tag in re.finditer(
            r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.DOTALL
        ):
            href = a_tag.group(1)
            text = a_tag.group(2).strip()
            if re.search(r'\.(pdf|doc|docx|xls|xlsx|zip|rar)(\?|$)', href, re.I) \
               or '附件' in text or '下载' in text:
                notice.attachments.append({
                    "url": urljoin(notice.url, href),
                    "name": text or href.split("/")[-1]
                })

        return len(parts) > 0

    async def fetch_detail(self, notice: BidNotice) -> BidNotice:
        """获取详情内容。ggzy.gov.cn 详情页内容直接在 /a/ 路径的页面中。
        先尝试 HTTP GET /a/ 页面提取标签内容；
        仍无内容再回退 Playwright。
        """
        import httpx

        # 第1步：直接从 /a/ 页面 HTTP 提取（最快路径）
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(notice.url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200 and len(resp.text) > 2000:
                    extracted = await self._extract_from_a_page(notice, resp.text)
                    if extracted and notice.content_text:
                        logger.info("ggzy /a/ HTTP 详情获取成功: %s (%d 字符)",
                                    notice.url, len(notice.content_text))
                        return notice
        except Exception as e:
            logger.debug("/a/ HTTP 获取失败: %s, fallback to browser", e)

        # 第2步：尝试 /b/ 路径（旧版 AJAX 路径，可能部分省站仍使用）
        b_url = notice.url.replace("/deal/html/a/", "/deal/html/b/")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(b_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200 and len(resp.text) > 200:
                    html = resp.text
                    m_content = re.search(
                        r'<div[^>]*class="detail_content"[^>]*>(.*?)</div>', html, re.DOTALL
                    )
                    if m_content:
                        content_html = m_content.group(1).strip()
                        if len(content_html) > 50:
                            notice.content_html = content_html
                            notice.content_text = re.sub(r'<[^>]+>', '', content_html).strip()
                            # 提取附件
                            for a in re.finditer(
                                r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                                html, re.DOTALL
                            ):
                                href = a.group(1)
                                text = a.group(2).strip()
                                if re.search(r'\.(pdf|doc|docx|xls|xlsx|zip|rar)(\?|$)', href, re.I) \
                                   or '附件' in text or '下载' in text:
                                    notice.attachments.append({
                                        "url": urljoin(notice.url, href),
                                        "name": text or href.split("/")[-1]
                                    })
                            logger.info("ggzy /b/ 详情获取成功: %s (内容 %d 字符)",
                                        notice.url, len(notice.content_text or ""))
                            return notice
        except Exception as e:
            logger.debug("/b/ 路径获取失败: %s", e)

        # 第3步：Fallback — 用 Playwright 打开并提取
        async with self.browser.new_page() as page:
            response = await page.goto(notice.url, wait_until="domcontentloaded", timeout=60000)
            if response and response.status >= 400:
                logger.warning("详情页 HTTP %s: %s", response.status, notice.url)
                return notice

            # 等待内容加载
            try:
                await page.wait_for_selector("h4.h4_o, .fully, iframe", timeout=20000)
                await page.wait_for_timeout(2000)
            except Exception:
                logger.debug("ggzy 详情页等待内容超时: %s", notice.url)

            detail_js = """
            (function() {
                var title = '';
                var contentHtml = '';
                var contentText = '';
                var attachments = [];

                // 先尝试 iframe 内提取
                var iframe = document.querySelector('iframe');
                if (iframe) {
                    var doc = iframe.contentDocument || iframe.contentWindow.document;
                    if (doc) {
                        var t = doc.querySelector('.detail_title, .detailContent, h1.title, h1');
                        if (t) title = t.innerText.trim();
                        var c = doc.querySelector('.detail_content, .detailContent, .article-content, .content, #detail_content');
                        if (c) { contentHtml = c.innerHTML; contentText = c.innerText.trim(); }
                        var container = c || doc.body;
                        if (container) {
                            for (var a of container.querySelectorAll('a[href]')) {
                                var href = a.href;
                                var text = (a.innerText || '').trim();
                                if (/\\.(pdf|doc|docx|xls|xlsx|zip|rar)(\\?|$)/i.test(href) || /附件|下载/.test(text)) {
                                    attachments.push({ url: href, name: text || href.split('/').pop() });
                                }
                            }
                        }
                        if (title || contentText) return JSON.stringify({ title: title, contentHtml: contentHtml, contentText: contentText, attachments: attachments });
                    }
                }

                // 主 DOM 提取 — h4.h4_o 标题 + fully_toggle_cont 内容
                var titleEl = document.querySelector('h4.h4_o, .detail_title, .article-title');
                if (titleEl) title = titleEl.innerText.trim();

                // 提取选中 tab 的内容
                var parts = [];
                var conts = document.querySelectorAll('.fully_toggle_cont');
                conts.forEach(function(cont) {
                    var items = cont.querySelectorAll('.fully_list li');
                    items.forEach(function(li) {
                        var t = (li.innerText || '').trim();
                        if (t && t.indexOf('暂无数据') === -1) parts.push(t);
                    });
                });

                // 备选选择器
                if (parts.length === 0) {
                    var c = document.querySelector('%s');
                    if (c) {
                        contentHtml = c.innerHTML;
                        contentText = c.innerText.trim();
                    }
                } else {
                    contentText = parts.join('\\n');
                    contentHtml = '<div class=\"ggzy_detail\">' + parts.map(function(p) { return '<p>' + p + '</p>'; }).join('') + '</div>';
                }

                // 附件
                var container = document.querySelector('%s') || document.body;
                for (var a of container.querySelectorAll('a[href]')) {
                    var href = a.href;
                    var text = (a.innerText || '').trim();
                    if (/\\.(pdf|doc|docx|xls|xlsx|zip|rar)(\\?|$)/i.test(href) || /附件|下载/.test(text)) {
                        attachments.push({ url: href, name: text || href.split('/').pop() });
                    }
                }
                return JSON.stringify({ title: title, contentHtml: contentHtml, contentText: contentText, attachments: attachments });
            })()
            """ % (
                DETAIL_CONTENT_SELECTOR.replace("'", "\\'"),
                DETAIL_CONTENT_SELECTOR.replace("'", "\\'"),
            )

            detail = await page.evaluate(detail_js)
            if detail.get("title"):
                notice.title = detail["title"]
            if detail.get("contentText"):
                notice.content_text = detail["contentText"]
            if detail.get("contentHtml"):
                notice.content_html = detail["contentHtml"]
            if detail.get("attachments"):
                notice.attachments = detail["attachments"]

        return notice


class GgzyNationalAdapter(GgzyBaseAdapter):
    """全国公共资源交易平台 — 交易公开列表。"""

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
                await page.wait_for_selector(DEAL_LINK_SELECTOR, timeout=20000)
            except Exception:
                logger.debug("交易列表选择器未匹配: %s", list_url)

            for link in await page.query_selector_all(DEAL_LINK_SELECTOR):
                if len(notices) >= max_items:
                    break
                notice = await self._parse_deal_link(link, page, list_url, seen)
                if notice:
                    notices.append(notice)

        return notices

    async def _parse_deal_link(
        self, link, page, list_url: str, seen: set[str]
    ) -> Optional[BidNotice]:
        href = await link.get_attribute("href")
        title = (await link.inner_text()).strip()
        if not href or not title or len(title) < 8:
            return None
        if href.startswith("javascript:"):
            return None

        full_url = urljoin(page.url, href)
        if full_url in seen:
            return None
        seen.add(full_url)

        publish_date = self._extract_date_from_url(full_url) or self._extract_date(title)
        category = NoticeCategory.TENDER
        if "中标" in title or "成交" in title or "结果" in title:
            category = NoticeCategory.WIN

        return BidNotice(
            title=title,
            url=full_url,
            source_site_id=self.site_id,
            source_site_name=self.site_name,
            source_url=list_url,
            publish_date=publish_date,
            category=category,
        )

    def _extract_date_from_url(self, url: str) -> Optional[datetime]:
        m = re.search(r"/(\d{8})/", url)
        if m:
            d = m.group(1)
            try:
                return datetime(int(d[:4]), int(d[4:6]), int(d[6:8]))
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
