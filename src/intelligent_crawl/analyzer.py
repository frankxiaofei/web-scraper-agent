"""页面语义分析 + 递归决策（规则层 + 可选 LLM）。"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from src.core.models import BidNotice, NoticeCategory, PageType
from src.intelligent_crawl.llm_client import LLMCrawlClient, create_llm_client
from src.intelligent_crawl.models import CrawlDecision, NextUrl, PageSemantics, SiteContext

logger = logging.getLogger(__name__)

# 招标相关关键词
BID_KEYWORDS = ("招标", "采购", "公告", "中标", "更正", "竞争性", "磋商", "询价", "成交", "谈判")
PAGINATION_KEYWORDS = ("下一页", "下页", "next", "more", "»", ">>", "后页")
RECURSION_HINTS = ("详见附件", "详见", "点击查看", "子包", "标段", "分包", "包件", "包号", "下载")
ATTACHMENT_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar")
EXCLUDE_TEXT = ("登录", "注册", "English", "首页", "退出", "忘记密码")
EXCLUDE_PATH = ("/login", "/register", "/signin", "/signup")


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}" + (
        f"?{parsed.query}" if parsed.query else ""
    )


def same_site(url: str, base_domain: str) -> bool:
    host = urlparse(url).netloc.lower()
    base = base_domain.lower().lstrip(".")
    return host == base or host.endswith("." + base) or base.endswith(host)


def has_bid_keyword(url: str, text: str) -> bool:
    combined = f"{url} {text}"
    return any(kw in combined for kw in BID_KEYWORDS)


def is_excluded_link(url: str, text: str) -> bool:
    if not url or url.startswith("javascript:"):
        return True
    lower = url.lower()
    if any(p in lower for p in EXCLUDE_PATH):
        return True
    if any(kw in text for kw in EXCLUDE_TEXT):
        return True
    return False


class SemanticAnalyzer:
    """页面语义分析器：规则层必可用，LLM 层可选增强。"""

    def __init__(
        self,
        max_depth: int = 3,
        llm_client: Optional[LLMCrawlClient] = None,
        use_llm: Optional[bool] = None,
    ) -> None:
        self.max_depth = max_depth
        self._llm = llm_client if llm_client is not None else create_llm_client()
        if use_llm is False:
            self._llm = None

    def analyze(
        self,
        semantics: PageSemantics,
        *,
        depth: int,
        site_context: SiteContext,
        visited: set[str],
    ) -> CrawlDecision:
        """分析页面并返回递归决策。"""
        if self._llm:
            llm_decision = self._llm.decide(semantics, depth=depth, site_context=site_context)
            if llm_decision and llm_decision.next_urls:
                llm_decision.extracted_notice = self._extract_notice(
                    semantics, depth=depth, site_context=site_context, page_type=llm_decision.page_type
                )
                return llm_decision

        return self._rule_analyze(semantics, depth=depth, site_context=site_context, visited=visited)

    def classify_page(self, semantics: PageSemantics) -> PageType:
        path = urlparse(semantics.url).path.lower()
        if any(path.endswith(ext) for ext in ATTACHMENT_EXT):
            return PageType.ATTACHMENT
        if semantics.list_row_count >= 5 and len(semantics.links) >= 8:
            return PageType.LIST
        if semantics.pagination_links and semantics.list_row_count >= 3:
            return PageType.LIST
        if len(semantics.body_text) > 300 and semantics.list_row_count < 5:
            return PageType.DETAIL
        if path in ("", "/") or path.endswith("/index.html"):
            return PageType.INDEX
        return PageType.UNKNOWN

    def _rule_analyze(
        self,
        semantics: PageSemantics,
        *,
        depth: int,
        site_context: SiteContext,
        visited: set[str],
    ) -> CrawlDecision:
        page_type = self.classify_page(semantics)
        next_urls: list[NextUrl] = []
        seen_local: set[str] = set()

        def add_next(url: str, text: str, child_depth: int, reason: str, priority: int) -> None:
            normalized = normalize_url(url)
            if not normalized or normalized in visited or normalized in seen_local:
                return
            if is_excluded_link(normalized, text):
                return
            same = same_site(normalized, site_context.base_domain)
            if not same and not has_bid_keyword(normalized, text):
                return
            if child_depth > self.max_depth:
                return
            seen_local.add(normalized)
            # 同域优先：降低 priority 数值
            prio = priority if same else priority + 3
            next_urls.append(NextUrl(url=normalized, reason=reason, priority=prio))

        # 列表/首页：分页 + 公告条目
        if page_type in (PageType.LIST, PageType.INDEX):
            for link in semantics.pagination_links:
                text = link.get("text", "")
                url = link.get("url", "")
                if any(kw.lower() in text.lower() for kw in PAGINATION_KEYWORDS) or re.search(
                    r"page=|index_\d|list_\d|PageNo=", url, re.I
                ):
                    add_next(url, text, depth, "pagination", 2)

            for link in semantics.links:
                url = link.get("url", "")
                text = link.get("text", "")
                if len(text) < 4:
                    continue
                if has_bid_keyword(url, text):
                    add_next(url, text, depth + 1, "list_item", 3)

        # 详情页：附件、标段、详见链接
        if page_type == PageType.DETAIL:
            body_hints = any(h in semantics.body_text for h in RECURSION_HINTS)
            for link in semantics.links:
                url = link.get("url", "")
                text = link.get("text", "")
                if any(ext in url.lower() for ext in ATTACHMENT_EXT) or "附件" in text or "下载" in text:
                    add_next(url, text, depth + 1, "attachment", 4)
                elif body_hints and has_bid_keyword(url, text):
                    add_next(url, text, depth + 1, "sub_link", 5)
                elif "详见" in text or "标段" in text or "包件" in text:
                    add_next(url, text, depth + 1, "section_link", 5)

        # 通用：招标关键词链接（深度未达上限）
        if depth < self.max_depth:
            for link in semantics.links:
                url = link.get("url", "")
                text = link.get("text", "")
                if has_bid_keyword(url, text):
                    add_next(url, text, depth + 1, "bid_keyword", 6)

        extracted = self._extract_notice(semantics, depth=depth, site_context=site_context, page_type=page_type)
        should_recurse = bool(next_urls) and depth < self.max_depth

        return CrawlDecision(
            page_type=page_type,
            should_recurse=should_recurse,
            next_urls=sorted(next_urls, key=lambda x: x.priority),
            extracted_notice=extracted,
        )

    def _extract_notice(
        self,
        semantics: PageSemantics,
        *,
        depth: int,
        site_context: SiteContext,
        page_type: PageType,
    ) -> Optional[BidNotice]:
        """从页面提取公告（详情/列表项/附件页）。"""
        if page_type not in (PageType.DETAIL, PageType.ATTACHMENT):
            # 列表页不单独存整页，由 orchestrator 解析条目
            if page_type == PageType.LIST:
                return None
            if page_type == PageType.UNKNOWN and len(semantics.body_text) < 200:
                return None

        title = semantics.title or semantics.url.split("/")[-1] or "未命名公告"
        category = NoticeCategory.TENDER
        if "中标" in title or "成交" in title:
            category = NoticeCategory.WIN
        elif "更正" in title or "变更" in title:
            category = NoticeCategory.CHANGE

        attachments = []
        for link in semantics.links:
            url = link.get("url", "")
            text = link.get("text", "")
            if any(ext in url.lower() for ext in ATTACHMENT_EXT) or "附件" in text or "下载" in text:
                attachments.append({"url": url, "name": text or url.split("/")[-1]})

        return BidNotice(
            title=title[:500],
            url=semantics.url,
            source_site_id=site_context.site_id,
            source_site_name=site_context.site_name,
            source_url=site_context.base_url,
            category=category,
            content_text=semantics.body_text[:50000] if semantics.body_text else None,
            content_html=semantics.html[:100000] if semantics.html else None,
            attachments=attachments,
            depth=depth,
            page_type=page_type,
            needs_recursion=False,
        )
