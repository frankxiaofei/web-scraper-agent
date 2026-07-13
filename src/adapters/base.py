"""站点适配器基类。"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from src.core.browser import BrowserPool
from src.core.models import BidNotice, ScrapeResult
from src.core.sync_cancel import SyncCancelledError, raise_if_sync_cancelled

logger = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """所有站点适配器的抽象基类。"""

    site_id: str = ""
    site_name: str = ""
    base_url: str = ""

    def __init__(self, site_config: dict[str, Any], browser_pool: BrowserPool) -> None:
        self.config = site_config
        self.site_id = site_config.get("id", "")
        self.site_name = site_config.get("name", "")
        self.base_url = site_config.get("url", "")
        self.browser = browser_pool
        self._schedule_defaults: dict[str, Any] = {}

    def set_schedule_defaults(self, defaults: dict[str, Any]) -> None:
        """由调度器注入 schedule.yaml 中的全局默认值。"""
        self._schedule_defaults = defaults

    def _pre_run_delay_seconds(self) -> float:
        """整次 run 开始前的冷却（sites.yaml min_delay_seconds）。"""
        if "min_delay_seconds" in self.config:
            return float(self.config["min_delay_seconds"])
        return float(self._schedule_defaults.get("default_min_delay_seconds", 0))

    def _min_delay_seconds(self) -> float:
        """分页/详情之间的请求间隔。"""
        return float(self._schedule_defaults.get("default_min_delay_seconds", 0))

    def _max_retries(self) -> int:
        if "max_retries" in self.config:
            return int(self.config["max_retries"])
        return int(self._schedule_defaults.get("max_retries", 3))

    @abstractmethod
    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        """抓取公告列表页，返回 BidNotice 列表。"""

    async def fetch_detail(self, notice: BidNotice) -> BidNotice:
        """可选：抓取详情页并填充正文等字段，子类按需覆盖。"""
        return notice

    def _should_fetch_detail(self, override: Optional[bool] = None) -> bool:
        if override is not None:
            return override
        return bool(self.config.get("fetch_detail", False))

    def _enable_recursive(self, override: Optional[bool] = None) -> bool:
        if override is not None:
            return override
        return bool(self.config.get("enable_recursive", False))

    def _enable_intelligent(self, override: Optional[bool] = None) -> bool:
        if override is not None:
            return override
        return bool(
            self.config.get("enable_intelligent_crawl")
            or self.config.get("enable_recursive", False)
        )

    def _max_crawl_depth(self) -> int:
        return int(self.config.get("max_crawl_depth", 3))

    def _max_intelligent_pages(self, max_items: int) -> int:
        hard_cap = int(self.config.get("max_intelligent_pages", 30))
        return min(max_items * 3, hard_cap)

    async def _expand_intelligent(
        self,
        notices: list[BidNotice],
        max_items: int,
        mongo_repo: Any = None,
        fetch_detail: bool = False,
    ) -> list[BidNotice]:
        """智能爬取扩展：语义分析 → BFS → MongoDB。"""
        from src.intelligent_crawl.orchestrator import IntelligentCrawlOrchestrator

        max_pages = self._max_intelligent_pages(max_items)
        orchestrator = IntelligentCrawlOrchestrator(
            browser=self.browser,
            site_config=self.config,
            adapter=self,
            max_depth=self._max_crawl_depth(),
            max_pages=max_pages,
            min_delay=self._min_delay_seconds(),
            mongo_repo=mongo_repo,
            fetch_detail=fetch_detail,
        )
        return await orchestrator.run_from_seeds(notices, max_pages=max_pages)

    async def _enrich_with_details(
        self, notices: list[BidNotice], fetch_detail: bool
    ) -> list[BidNotice]:
        if not fetch_detail or not notices:
            return notices

        min_delay = self._min_delay_seconds()
        enriched: list[BidNotice] = []
        for i, notice in enumerate(notices):
            raise_if_sync_cancelled()
            if i > 0 and min_delay > 0:
                await asyncio.sleep(min_delay)
                raise_if_sync_cancelled()
            try:
                from src.core.crawl_run import emit_crawl_event

                emit_crawl_event(
                    "fetch_detail",
                    f"抓取详情页 ({i + 1}/{len(notices)})",
                    data={"url": notice.url},
                )
                detail = await self.fetch_detail(notice)
                from src.core.agri_classifier import apply_agri_classification_to_notice
                from src.core.key_info_extractor import apply_key_info_to_notice

                detail = apply_key_info_to_notice(detail)
                enriched.append(apply_agri_classification_to_notice(detail, rate_limit=True))
            except Exception as e:
                logger.warning("站点 %s 详情抓取失败 %s: %s", self.site_id, notice.url, e)
                enriched.append(notice)
        return enriched

    async def run(
        self,
        max_items: int = 20,
        fetch_detail: Optional[bool] = None,
        recursive: Optional[bool] = None,
        intelligent: Optional[bool] = None,
        mongo_repo: Any = None,
    ) -> ScrapeResult:
        """执行抓取并包装为 ScrapeResult（含请求间隔与失败重试）。"""
        from src.core.crawl_rules import load_crawl_rule

        rule = load_crawl_rule(self.config)
        min_delay = self._pre_run_delay_seconds()
        skip_pre_delay = False
        if rule:
            from src.core.rule_executor import RuleExecutor

            skip_pre_delay = RuleExecutor(self.browser, self.config)._is_pure_api_list(rule)
        if min_delay > 0 and not skip_pre_delay:
            from src.core.crawl_run import emit_crawl_event

            emit_crawl_event("delay", f"请求前等待 {min_delay:.1f}s", data={"seconds": min_delay})
            logger.debug("站点 %s 请求前等待 %.1fs", self.site_id, min_delay)
            raise_if_sync_cancelled()
            await asyncio.sleep(min_delay)
            raise_if_sync_cancelled()

        max_retries = max(1, self._max_retries())
        last_error: Optional[str] = None
        want_detail = self._should_fetch_detail(fetch_detail)
        want_intelligent = self._enable_intelligent(intelligent if intelligent is not None else recursive)

        for attempt in range(max_retries):
            raise_if_sync_cancelled()
            start = time.perf_counter()
            try:
                from src.core.crawl_run import emit_crawl_event

                rule = load_crawl_rule(self.config)
                list_url = (
                    (rule.entry_url if rule else None)
                    or self.base_url
                )
                emit_crawl_event(
                    "fetch_list",
                    f"抓取列表页 (尝试 {attempt + 1}/{max_retries})",
                    data={"url": list_url},
                )
                if rule and rule.list_page:
                    from src.core.rule_executor import RuleExecutor

                    executor = RuleExecutor(self.browser, self.config)
                    notices = await executor.execute(rule, max_items)
                else:
                    notices = await self.fetch_list(max_items=max_items)
                notices = await self._enrich_with_details(notices, want_detail)
                if want_intelligent:
                    emit_crawl_event("intelligent", "启动智能爬取扩展")
                    notices = await self._expand_intelligent(
                        notices, max_items, mongo_repo=mongo_repo, fetch_detail=want_detail
                    )
                duration = time.perf_counter() - start
                logger.info(
                    "站点 %s 抓取完成: %d 条%s%s, 耗时 %.2fs",
                    self.site_id,
                    len(notices),
                    "（含详情）" if want_detail else "",
                    "（含智能爬取）" if want_intelligent else "",
                    duration,
                )
                return ScrapeResult(
                    site_id=self.site_id,
                    success=True,
                    notices=notices,
                    duration_seconds=duration,
                )
            except SyncCancelledError:
                duration = time.perf_counter() - start
                return ScrapeResult(
                    site_id=self.site_id,
                    success=False,
                    notices=[],
                    error="cancelled",
                    duration_seconds=duration,
                )
            except Exception as e:
                duration = time.perf_counter() - start
                last_error = str(e)
                logger.exception(
                    "站点 %s 抓取失败 (尝试 %d/%d): %s",
                    self.site_id,
                    attempt + 1,
                    max_retries,
                    e,
                )
                if attempt < max_retries - 1:
                    backoff = 2**attempt
                    from src.core.crawl_run import emit_crawl_event

                    emit_crawl_event("retry", f"{backoff}s 后重试 (第 {attempt + 2} 次)", level="warn")
                    logger.info("站点 %s %.0fs 后重试", self.site_id, backoff)
                    raise_if_sync_cancelled()
                    await asyncio.sleep(backoff)

        duration = time.perf_counter() - start
        return ScrapeResult(
            site_id=self.site_id,
            success=False,
            error=last_error or "未知错误",
            duration_seconds=duration,
        )
