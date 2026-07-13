"""智能爬取编排器：BFS 队列、深度控制、去重、MongoDB 持久化。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from src.adapters.base import BaseAdapter
from src.core.browser import BrowserPool
from src.core.models import BidNotice, PageType
from src.core.url_utils import is_likely_detail_url, is_likely_list_url
from src.intelligent_crawl.analyzer import SemanticAnalyzer, normalize_url, same_site
from src.intelligent_crawl.models import CrawlTask, PageSemantics, SiteContext

logger = logging.getLogger(__name__)

_NETWORK_ERROR_MARKERS = (
    "ERR_NETWORK_CHANGED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_ADDRESS_UNREACHABLE",
    "net::ERR_",
    "Timeout",
)
_MAX_LOAD_FAILURES = 2

_EXTRACT_PAGE_JS = """() => {
    const links = [];
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href;
        if (!href || href.startsWith('javascript:')) continue;
        links.push({ url: href, text: (a.innerText || a.textContent || '').trim().slice(0, 200) });
    }
    const bodyText = (document.body && document.body.innerText) ? document.body.innerText.slice(0, 8000) : '';
    const html = document.documentElement ? document.documentElement.outerHTML.slice(0, 50000) : '';
    const titleEl = document.querySelector('h1, h2, .title, .vF_detail_header h2');
    const title = titleEl ? titleEl.innerText.trim() : (document.title || '');
    const listRows = document.querySelectorAll('table tr a, ul li a, .list-item a').length;
    const paginationLinks = links.filter(l =>
        /下一页|下页|next|»|>>|后页|more/i.test(l.text) || /page=|index_\\d|list_\\d|PageNo=/i.test(l.url)
    );
    return { links, bodyText, html, title, listRows, paginationLinks, pageTitle: document.title };
}"""

_LIST_ITEMS_JS = """() => {
    const out = [];
    const anchors = document.querySelectorAll(
        'ul li a, table tr a, .list-item a, .news-list a, .vT-srch-result-list a'
    );
    for (const a of anchors) {
        const text = (a.innerText || '').trim();
        const href = a.href;
        if (!text || text.length < 4 || !href || href.startsWith('javascript:')) continue;
        if (/首页|登录|注册|English/i.test(text)) continue;
        out.push({ title: text.slice(0, 300), url: href });
    }
    return out;
}"""


class IntelligentCrawlOrchestrator:
    """BFS 智能爬取：analyzer 决策 → 入队 → MongoDB 写入。"""

    def __init__(
        self,
        browser: BrowserPool,
        site_config: dict[str, Any],
        adapter: BaseAdapter,
        *,
        max_depth: int = 3,
        max_pages: int = 30,
        min_delay: float = 0,
        goto_timeout_ms: int = 30_000,
        mongo_repo: Any = None,
        fetch_detail: bool = False,
    ) -> None:
        self.browser = browser
        self.config = site_config
        self.adapter = adapter
        self.fetch_detail = fetch_detail or bool(site_config.get("fetch_detail", False))
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.min_delay = min_delay
        self.goto_timeout_ms = int(
            site_config.get("goto_timeout_ms", goto_timeout_ms)
        )
        self.mongo_repo = mongo_repo

        base_url = site_config.get("url", "")
        self.site_context = SiteContext(
            site_id=adapter.site_id,
            site_name=adapter.site_name,
            base_url=base_url,
            base_domain=urlparse(base_url).netloc,
        )
        self.analyzer = SemanticAnalyzer(max_depth=max_depth)
        self._visited: set[str] = set()
        self._load_failures: dict[str, int] = {}
        self._session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]

    async def run_from_seeds(
        self,
        seed_notices: list[BidNotice],
        *,
        max_pages: Optional[int] = None,
    ) -> list[BidNotice]:
        """从种子公告（列表项）出发，BFS 扩展并返回所有发现的公告。"""
        limit = max_pages or self.max_pages
        queue: list[CrawlTask] = []
        results: list[BidNotice] = []
        seen_notice_urls: set[str] = set()

        # 加载 MongoDB 已访问 URL
        if self.mongo_repo:
            persisted = self.mongo_repo.load_visited_urls(self.site_context.site_id)
            self._visited.update(persisted)

        if self.mongo_repo:
            self.mongo_repo.start_session(
                self._session_id,
                site_id=self.site_context.site_id,
                max_depth=self.max_depth,
                max_pages=limit,
            )

        # 种子入队
        for notice in seed_notices:
            notice.depth = max(notice.depth, 1)
            notice.page_type = notice.page_type or PageType.DETAIL
            self._mark_visited(notice.url)
            seen_notice_urls.add(notice.url)
            results.append(notice)
            queue.append(
                CrawlTask(
                    priority=1,
                    url=notice.url,
                    depth=notice.depth,
                    parent_url=notice.source_url or self.site_context.base_url,
                    reason="seed",
                    link_text=notice.title,
                )
            )

        pages_crawled = 0
        while queue and pages_crawled < limit:
            task = queue.pop(0)
            normalized = normalize_url(task.url)
            if not normalized or self._should_skip_url(normalized, pages_crawled):
                continue

            if self.min_delay > 0 and pages_crawled > 0:
                await asyncio.sleep(self.min_delay)

            semantics = await self._fetch_page(task.url)
            if not semantics:
                continue

            self._mark_visited(task.url)
            pages_crawled += 1

            decision = self.analyzer.analyze(
                semantics,
                depth=task.depth,
                site_context=self.site_context,
                visited=self._visited,
            )

            # 列表页：解析条目作为公告
            if decision.page_type == PageType.LIST:
                list_notices = await self._parse_list_items(task.url, parent_url=task.url, depth=task.depth)
                for ln in list_notices:
                    if ln.url not in seen_notice_urls:
                        ln.parent_url = task.url
                        ln.depth = task.depth + 1
                        ln.page_type = PageType.DETAIL
                        if is_likely_list_url(ln.url):
                            ln.page_type = PageType.LIST
                            seen_notice_urls.add(ln.url)
                            if task.depth + 1 <= self.max_depth:
                                queue.append(
                                    CrawlTask(
                                        priority=3,
                                        url=ln.url,
                                        depth=task.depth + 1,
                                        parent_url=task.url,
                                        reason="list_nav",
                                        link_text=ln.title,
                                    )
                                )
                            continue
                        ln = await self._maybe_fetch_detail(ln)
                        results.append(ln)
                        seen_notice_urls.add(ln.url)
                        if ln.content_text or ln.content_html:
                            self._save_notice(ln, crawl_reason="list_item")
                        if task.depth + 1 <= self.max_depth:
                            queue.append(
                                CrawlTask(
                                    priority=3,
                                    url=ln.url,
                                    depth=task.depth + 1,
                                    parent_url=task.url,
                                    reason="list_item",
                                    link_text=ln.title,
                                )
                            )

            # 详情/附件：提取公告
            elif decision.extracted_notice:
                notice = decision.extracted_notice
                notice.parent_url = task.parent_url or None
                notice.depth = task.depth
                notice.source_url = task.parent_url or self.site_context.base_url
                notice.needs_recursion = decision.should_recurse

                # 适配器 enrichment
                if decision.page_type == PageType.DETAIL:
                    notice = await self._maybe_fetch_detail(notice)

                if notice.url not in seen_notice_urls:
                    results.append(notice)
                    seen_notice_urls.add(notice.url)
                    if notice.content_text or notice.content_html or is_likely_detail_url(notice.url):
                        self._save_notice(notice, crawl_reason=task.reason)

            # 入队下一跳
            if decision.should_recurse:
                for nxt in decision.next_urls:
                    if normalize_url(nxt.url) not in self._visited:
                        queue.append(
                            CrawlTask(
                                priority=nxt.priority,
                                url=nxt.url,
                                depth=task.depth + 1 if nxt.reason != "pagination" else task.depth,
                                parent_url=task.url,
                                reason=nxt.reason,
                                link_text="",
                            )
                        )
                queue.sort(key=lambda t: t.priority)

            if len(results) >= limit * 2:
                break

        if self.mongo_repo:
            self.mongo_repo.finish_session(
                self._session_id,
                pages_crawled=pages_crawled,
                notices_saved=len(results),
                status="completed",
            )

        logger.info(
            "站点 %s 智能爬取: 种子 %d → 共 %d 条, 爬页 %d",
            self.site_context.site_id,
            len(seed_notices),
            len(results),
            pages_crawled,
        )
        return results

    def _is_network_error(self, exc: Exception) -> bool:
        msg = str(exc)
        return any(marker in msg for marker in _NETWORK_ERROR_MARKERS)

    def _should_skip_url(self, normalized: str, pages_crawled: int) -> bool:
        if self._load_failures.get(normalized, 0) >= _MAX_LOAD_FAILURES:
            return True
        return normalized in self._visited and pages_crawled > 0

    def _record_load_failure(self, url: str, exc: Exception) -> None:
        normalized = normalize_url(url)
        if not normalized:
            return
        if not self._is_network_error(exc):
            logger.warning("爬取页面失败 %s: %s", url, exc)
            return
        fails = self._load_failures.get(normalized, 0) + 1
        self._load_failures[normalized] = fails
        if fails >= _MAX_LOAD_FAILURES:
            logger.warning(
                "跳过 URL（网络错误 %d 次）: %s — %s",
                fails,
                url,
                exc,
            )
            self._mark_visited(url)
        else:
            logger.warning("爬取页面失败 %s (%d/%d): %s", url, fails, _MAX_LOAD_FAILURES, exc)

    async def _fetch_page(self, url: str) -> Optional[PageSemantics]:
        normalized = normalize_url(url)
        if normalized and self._load_failures.get(normalized, 0) >= _MAX_LOAD_FAILURES:
            return None

        async with self.browser.new_page() as page:
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.goto_timeout_ms,
                )
                if response and response.status >= 400:
                    logger.debug("页面 HTTP %s: %s", response.status, url)
                    return None
                if normalized:
                    self._load_failures.pop(normalized, None)
                raw = await page.evaluate(_EXTRACT_PAGE_JS)
                return PageSemantics(
                    url=url,
                    title=raw.get("title") or raw.get("pageTitle") or "",
                    body_text=raw.get("bodyText") or "",
                    html=raw.get("html") or "",
                    links=raw.get("links") or [],
                    pagination_links=raw.get("paginationLinks") or [],
                    list_row_count=int(raw.get("listRows") or 0),
                )
            except Exception as e:
                self._record_load_failure(url, e)
                return None

    async def _parse_list_items(
        self, list_url: str, *, parent_url: str, depth: int
    ) -> list[BidNotice]:
        notices: list[BidNotice] = []
        async with self.browser.new_page() as page:
            try:
                await page.goto(
                    list_url,
                    wait_until="domcontentloaded",
                    timeout=self.goto_timeout_ms,
                )
                items = await page.evaluate(_LIST_ITEMS_JS)
                for item in (items or [])[:10]:
                    url = item.get("url", "")
                    if not url or not same_site(url, self.site_context.base_domain):
                        continue
                    if is_likely_list_url(url):
                        continue
                    notices.append(
                        BidNotice(
                            title=item.get("title", ""),
                            url=url,
                            source_site_id=self.site_context.site_id,
                            source_site_name=self.site_context.site_name,
                            source_url=list_url,
                            parent_url=parent_url,
                            depth=depth + 1,
                            page_type=PageType.DETAIL,
                        )
                    )
            except Exception as e:
                logger.debug("列表页解析失败 %s: %s", list_url, e)
        return notices

    async def _maybe_fetch_detail(self, notice: BidNotice) -> BidNotice:
        if not self.fetch_detail or not hasattr(self.adapter, "fetch_detail"):
            return notice
        try:
            detail = await self.adapter.fetch_detail(notice)
            from src.core.key_info_extractor import apply_key_info_to_notice

            return apply_key_info_to_notice(detail)
        except Exception as e:
            logger.debug("详情 enrichment 失败 %s: %s", notice.url, e)
            return notice

    def _mark_visited(self, url: str) -> None:
        normalized = normalize_url(url)
        if not normalized:
            return
        self._visited.add(normalized)
        if self.mongo_repo:
            self.mongo_repo.mark_visited(normalized, site_id=self.site_context.site_id)

    def _save_notice(self, notice: BidNotice, *, crawl_reason: str = "") -> None:
        if not self.mongo_repo:
            return
        has_content = bool(notice.content_text or notice.content_html)
        if notice.page_type == PageType.LIST and not has_content:
            logger.debug("跳过保存列表页（无正文）: %s", notice.url)
            return
        if is_likely_list_url(notice.url) and not has_content:
            logger.debug("跳过保存导航/列表 URL（无正文）: %s", notice.url)
            return
        if not notice.content_hash:
            date_part = ""
            if notice.publish_date:
                date_part = notice.publish_date.strftime("%Y-%m-%d")
            raw = f"{notice.url}|{notice.title}|{date_part}"
            notice.content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        from src.core.agri_classifier import apply_agri_classification_to_notice

        apply_agri_classification_to_notice(notice, rate_limit=True)
        doc_extra = {"crawl_reason": crawl_reason, "session_id": self._session_id}
        self.mongo_repo.upsert_notice(notice, extra=doc_extra)
