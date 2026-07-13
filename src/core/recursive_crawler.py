"""[已废弃] 旧版递归爬取 — 请使用 src.intelligent_crawl.orchestrator。"""

from __future__ import annotations

import warnings

warnings.warn(
    "src.core.recursive_crawler 已废弃，请改用 src.intelligent_crawl",
    DeprecationWarning,
    stacklevel=2,
)

# 向后兼容：旧 import 仍可工作
from src.intelligent_crawl.orchestrator import IntelligentCrawlOrchestrator as RecursiveCrawler
from src.intelligent_crawl.analyzer import SemanticAnalyzer as SemanticDecisionEngine
from src.intelligent_crawl.models import PageSemantics as PageInfo

__all__ = ["RecursiveCrawler", "SemanticDecisionEngine", "PageInfo"]
