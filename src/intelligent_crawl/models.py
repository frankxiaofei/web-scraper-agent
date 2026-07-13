"""智能爬取数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.core.models import BidNotice, PageType


@dataclass
class SiteContext:
    """站点上下文，供语义分析使用。"""

    site_id: str
    site_name: str
    base_url: str
    base_domain: str


@dataclass
class PageSemantics:
    """页面语义摘要（由浏览器提取）。"""

    url: str
    title: str = ""
    body_text: str = ""
    html: str = ""
    links: list[dict[str, str]] = field(default_factory=list)
    pagination_links: list[dict[str, str]] = field(default_factory=list)
    list_row_count: int = 0


@dataclass
class NextUrl:
    """待爬取的下一跳链接。"""

    url: str
    reason: str
    priority: int = 5  # 数值越小优先级越高


@dataclass
class CrawlDecision:
    """语义分析决策结果。"""

    page_type: PageType = PageType.UNKNOWN
    should_recurse: bool = False
    next_urls: list[NextUrl] = field(default_factory=list)
    extracted_notice: Optional[BidNotice] = None


@dataclass(order=True)
class CrawlTask:
    """BFS 队列任务（按 priority 排序）。"""

    priority: int
    url: str = field(compare=False)
    depth: int = field(compare=False, default=0)
    parent_url: str = field(compare=False, default="")
    reason: str = field(compare=False, default="")
    link_text: str = field(compare=False, default="")


@dataclass
class CrawlSessionInfo:
    """爬取会话元数据。"""

    session_id: str
    site_id: str
    max_depth: int
    max_pages: int
    pages_crawled: int = 0
    notices_saved: int = 0
    status: str = "running"
    extra: dict[str, Any] = field(default_factory=dict)
