"""通用规则驱动适配器：仅依赖 config/crawl_rules/{site_id}.yaml。"""

from __future__ import annotations

import logging
import re
import time
from html import unescape
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from src.adapters.base import BaseAdapter
from src.core.crawl_rules import DetailRule, load_crawl_rule
from src.core.models import BidNotice
from src.core.publish_date_utils import reconcile_publish_date

logger = logging.getLogger(__name__)

_DEFAULT_TITLE_SELECTOR = ".title, h1, h2"
_DEFAULT_CONTENT_SELECTOR = ".gg_info, #content, .content, #nbprint"

_DETAIL_EXTRACT_JS = """([titleSel, contentSel, metaSel]) => {
    const pick = (sel) => {
        for (const part of sel.split(',')) {
            const el = document.querySelector(part.trim());
            if (el) return el;
        }
        return null;
    };
    const titleEl = pick(titleSel);
    const contentEl = pick(contentSel);
    const metaEl = metaSel ? pick(metaSel) : null;
    const title = titleEl ? titleEl.innerText.trim() : '';
    const contentHtml = contentEl ? contentEl.innerHTML : '';
    const contentText = contentEl ? contentEl.innerText.trim() : '';
    const metaText = metaEl ? metaEl.innerText.trim() : '';
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
    return { title, contentHtml, contentText, metaText, attachments };
}"""


def _url_matches_pattern(url: str, pattern: Optional[str]) -> bool:
    if not pattern:
        return True
    return any(part in url for part in pattern.split("|") if part)


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _get_nested_field(data: Any, spec: str) -> Any:
    for key in spec.split("|"):
        if isinstance(data, dict):
            val = data.get(key.strip())
            if val not in (None, ""):
                return val
    return None


def _extract_meta_field(text: str, labels: list[str]) -> Optional[str]:
    for label in labels:
        m = re.search(rf"{re.escape(label)}[：:\s]*([^\n\r，,；;]+)", text)
        if m:
            value = m.group(1).strip()
            if value and len(value) < 200:
                return value
    return None


class GenericRuleAdapter(BaseAdapter):
    """adapter=generic 且已配置 crawl_rules 时的规则驱动适配器。"""

    def _min_delay_seconds(self) -> float:
        rule = load_crawl_rule(self.config)
        if rule and rule.limits.rate_limit_seconds > 0:
            return rule.limits.rate_limit_seconds
        return super()._min_delay_seconds()

    def _detail_rule(self) -> Optional[DetailRule]:
        rule = load_crawl_rule(self.config)
        return rule.detail if rule else None

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        logger.warning(
            "站点 %s 使用 generic 适配器但未配置 crawl_rules.list_page，无法抓取",
            self.site_id,
        )
        return []

    async def fetch_detail(self, notice: BidNotice) -> BidNotice:
        detail = self._detail_rule()
        if not detail or not detail.fetch_detail:
            return notice
        if not _url_matches_pattern(notice.url, detail.url_pattern):
            return notice

        if detail.strategy == "api":
            return await self._fetch_detail_api(notice, detail)
        return await self._fetch_detail_dom(notice, detail)

    async def _fetch_detail_api(self, notice: BidNotice, detail: DetailRule) -> BidNotice:
        base_url = detail.api_base_url or self.base_url
        path_template = detail.api_path_template or "/BidAnnouncementSummary/getInfo/{id}"
        id_param = detail.api_id_param or "id"

        parsed = urlparse(notice.url)
        row_id = parse_qs(parsed.query).get(id_param, [None])[0]
        if not row_id:
            logger.warning("站点 %s 详情 URL 缺少 %s: %s", self.site_id, id_param, notice.url)
            return notice

        api_url = f"{base_url.rstrip('/')}{path_template.format(id=row_id)}"
        api_url = f"{api_url}?time={int(time.time() * 1000)}"

        try:
            from src.core.http_api_client import fetch_json_url

            body = await fetch_json_url(
                api_url,
                site_url=self.base_url,
                entry_url=self.base_url,
            )
        except Exception as exc:
            logger.warning("站点 %s 详情 HTTP API 失败: %s", self.site_id, exc)
            return notice

        data = body.get("data") if isinstance(body, dict) else body
        if not isinstance(data, dict):
            return notice

        title = _get_nested_field(data, detail.api_title_field or "title")
        content_html = _get_nested_field(
            data, detail.api_content_field or "announcementContent|content"
        )
        if title and notice.title:
            api_title = str(title).strip()
            try:
                from src.core.powerchina_notice import titles_match

                if not titles_match(notice.title, api_title):
                    logger.warning(
                        "站点 %s getInfo 标题不一致 id=%s expected=%s api=%s",
                        self.site_id,
                        row_id,
                        notice.title[:60],
                        api_title[:60],
                    )
                    return notice
            except ImportError:
                pass
        if title:
            notice.title = str(title).strip()
        if content_html:
            notice.content_html = str(content_html)
            notice.content_text = _html_to_text(notice.content_html)
        elif detail.api_base_url and "powerchina" in detail.api_base_url:
            try:
                from src.core.powerchina_notice import resolve_picture_url
                from src.core.pdf_content import download_pdf_text

                picture_url, _ = resolve_picture_url(data)
                if picture_url:
                    pdf_result = await download_pdf_text(
                        picture_url,
                        referer=self.base_url or "https://bid.powerchina.cn",
                    )
                    if pdf_result.get("ok") and pdf_result.get("content_text"):
                        notice.content_text = pdf_result["content_text"]
                        logger.debug(
                            "站点 %s getInfo 无 HTML，已从 PDF 提取正文 id=%s",
                            self.site_id,
                            row_id,
                        )
            except Exception as exc:
                logger.debug("站点 %s PDF 正文回退失败 id=%s: %s", self.site_id, row_id, exc)

        if notice.content_text:
            notice.purchaser = _extract_meta_field(
                notice.content_text, ["采购人", "招标人", "采购单位"]
            )
            notice.agency = _extract_meta_field(notice.content_text, ["代理机构", "招标代理"])
            notice.budget = _extract_meta_field(
                notice.content_text, ["预算金额", "最高限价", "控制价", "估算价"]
            )
        notice.publish_date = reconcile_publish_date(
            publish_date=notice.publish_date,
            content_text=notice.content_text,
        )
        return notice

    async def _fetch_detail_dom(self, notice: BidNotice, detail: DetailRule) -> BidNotice:
        title_selector = detail.title_selector or _DEFAULT_TITLE_SELECTOR
        content_selector = detail.content_selector or _DEFAULT_CONTENT_SELECTOR
        wait_selector = detail.wait_for or content_selector
        eval_args = [title_selector, content_selector, detail.meta_selector or ""]

        async with self.browser.new_page() as page:
            response = await page.goto(notice.url, wait_until="domcontentloaded", timeout=60000)
            if response and response.status >= 400:
                logger.warning("详情页 HTTP %s: %s", response.status, notice.url)
                return notice

            if detail.iframe_selector:
                frame = page.frame_locator(detail.iframe_selector)
                try:
                    first_wait = wait_selector.split(",")[0].strip()
                    await frame.locator(first_wait).first.wait_for(timeout=15000)
                except Exception:
                    logger.debug("iframe 详情等待选择器未匹配: %s", notice.url)
                detail_data = await frame.locator("body").first.evaluate(
                    _DETAIL_EXTRACT_JS, eval_args
                )
            else:
                try:
                    await page.wait_for_selector(wait_selector, timeout=15000)
                except Exception:
                    logger.debug("详情等待选择器未匹配: %s", notice.url)
                detail_data = await page.evaluate(_DETAIL_EXTRACT_JS, eval_args)

        if detail_data.get("title"):
            notice.title = detail_data["title"]
        if detail_data.get("contentText"):
            notice.content_text = detail_data["contentText"]
        if detail_data.get("contentHtml"):
            notice.content_html = detail_data["contentHtml"]
        if detail_data.get("attachments"):
            notice.attachments = detail_data["attachments"]

        meta_text = detail_data.get("metaText") or notice.content_text or ""
        notice.purchaser = _extract_meta_field(meta_text, ["采购人", "招标人", "采购单位"])
        notice.agency = _extract_meta_field(meta_text, ["代理机构", "招标代理"])
        notice.budget = _extract_meta_field(
            meta_text, ["预算金额", "最高限价", "控制价", "估算价"]
        )
        notice.publish_date = reconcile_publish_date(
            publish_date=notice.publish_date,
            content_text=notice.content_text,
        )
        return notice
