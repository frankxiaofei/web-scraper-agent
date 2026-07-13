"""AgentExecutor: 按 CrawlPlan 执行爬取并 emit 事件。"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional

from src.agent.events import (
    DONE,
    ERROR,
    EXTRACT,
    PAGE_LOAD,
    RECURSE,
    SAVE,
    make_event,
)
from src.agent.models import AgentEvent, CrawlPlan, ListStrategy
from src.core.browser import BrowserPool
from src.core.models import BidNotice, PageType, ScrapeResult
from src.core.pipeline import Pipeline
from src.core.scheduler import get_adapter_class
from src.core.site_sync import get_schedule_defaults, should_fetch_detail
from src.core.sync_status import get_sync_status_store
from src.db.mongo_repository import create_mongo_repository

logger = logging.getLogger(__name__)

_LIST_EXTRACT_JS = """(selector) => {
    const out = [];
    const anchors = document.querySelectorAll(selector || 'ul li a, table tr a, .list-item a');
    for (const a of anchors) {
        const text = (a.innerText || '').trim();
        const href = a.href;
        if (!text || text.length < 4 || !href || href.startsWith('javascript:')) continue;
        if (/首页|登录|注册|English/i.test(text)) continue;
        out.push({ title: text.slice(0, 300), url: href });
    }
    return out;
}"""


class AgentExecutor:
    """按 plan 执行爬取，每步 yield AgentEvent。"""

    def __init__(
        self,
        browser: BrowserPool,
        pipeline: Pipeline,
        site_config: dict[str, Any],
        plan: CrawlPlan,
        *,
        max_items: int = 10,
        emit: Optional[Callable[[AgentEvent], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.browser = browser
        self.pipeline = pipeline
        self.site_config = site_config
        self.plan = plan
        self.max_items = max_items
        self._emit = emit or (lambda e: None)
        self._cancelled = cancelled or (lambda: False)

    def _emit_event(self, event: AgentEvent) -> None:
        self._emit(event)

    def _check_cancel(self) -> bool:
        if self._cancelled():
            self._emit_event(make_event(ERROR, "任务已取消", level="warn"))
            return True
        return False

    async def run(self) -> dict[str, Any]:
        """执行爬取并返回结果摘要。"""
        site_id = self.plan.site_id
        status_store = get_sync_status_store()
        status_store.mark_syncing(
            site_id,
            self.plan.site_name or site_id,
            self.plan.base_url,
        )
        started = time.monotonic()

        settings = self.pipeline._settings
        mongo_repo = create_mongo_repository(
            uri=settings.mongodb_uri,
            db_name=settings.mongodb_db,
            collection_name=settings.mongodb_collection,
        )

        try:
            if self._check_cancel():
                return {"success": False, "error": "cancelled"}

            if self.plan.use_existing_adapter and self.plan.adapter_name:
                result = await self._run_with_adapter(mongo_repo)
            else:
                result = await self._run_dynamic(mongo_repo)

            if self._check_cancel():
                return {"success": False, "error": "cancelled"}

            elapsed = int(time.monotonic() - started)
            if not result.success:
                status_store.mark_failed(
                    site_id, error=result.error or "未知错误", duration_seconds=elapsed
                )
                self._emit_event(
                    make_event(ERROR, f"爬取失败: {result.error}", level="error")
                )
                return {
                    "success": False,
                    "error": result.error,
                    "notices_count": 0,
                    "new_count": 0,
                }

            processed = self.pipeline.process(result)
            saved = self.pipeline.save(processed)
            new_count = len(processed.new) + len(processed.enriched)

            self._emit_event(
                make_event(
                    SAVE,
                    f"入库完成: 共 {len(result.notices)} 条, 新增/补全 {new_count} 条",
                    level="success",
                    data={
                        "notices_count": len(result.notices),
                        "new_count": new_count,
                        "saved": saved,
                    },
                )
            )

            status_store.mark_completed(
                site_id,
                notices_count=len(result.notices),
                new_count=new_count,
                duration_seconds=elapsed,
            )

            summary = {
                "success": True,
                "notices_count": len(result.notices),
                "new_count": new_count,
                "site_id": site_id,
            }
            self._emit_event(
                make_event(
                    DONE,
                    f"爬取完成: {self.plan.site_name} — {len(result.notices)} 条公告",
                    level="success",
                    data=summary,
                )
            )
            return summary

        except Exception as e:
            logger.exception("Agent 执行异常: %s", site_id)
            elapsed = int(time.monotonic() - started)
            status_store.mark_failed(site_id, error=str(e), duration_seconds=elapsed)
            self._emit_event(make_event(ERROR, f"执行异常: {e}", level="error"))
            return {"success": False, "error": str(e)}
        finally:
            if mongo_repo:
                mongo_repo.close()

    async def _run_with_adapter(self, mongo_repo: Any) -> ScrapeResult:
        adapter_name = self.plan.adapter_name or self.site_config.get("adapter", "generic")
        self._emit_event(
            make_event(
                EXTRACT,
                f"使用已有适配器「{adapter_name}」执行爬取 (intelligent=True)",
                level="info",
                data={"adapter": adapter_name},
            )
        )

        schedule_defaults = get_schedule_defaults()
        fetch_detail = should_fetch_detail(self.site_config)
        intelligent = bool(self.site_config.get("enable_recursive"))

        adapter_cls = get_adapter_class(adapter_name)
        adapter = adapter_cls(self.site_config, self.browser)
        adapter.set_schedule_defaults(schedule_defaults)

        return await adapter.run(
            max_items=self.max_items,
            fetch_detail=True if fetch_detail else None,
            intelligent=True if intelligent else None,
            mongo_repo=mongo_repo,
        )

    async def _run_dynamic(self, mongo_repo: Any) -> ScrapeResult:
        """通用 DOM 动态爬取。"""
        list_url = self.plan.list_url or self.plan.base_url
        self._emit_event(
            make_event(PAGE_LOAD, f"加载列表页: {list_url}", level="info", data={"url": list_url})
        )

        selector = "ul li a, table tr a, .list-item a, .news-list a"
        if self.plan.selectors:
            selector = self.plan.selectors.get("list_item", selector)

        notices: list[BidNotice] = []
        detail_re = None
        if self.plan.detail_pattern:
            try:
                detail_re = re.compile(self.plan.detail_pattern)
            except re.error:
                pass

        async with self.browser.new_page() as page:
            try:
                await page.goto(
                    list_url,
                    wait_until="domcontentloaded",
                    timeout=int(self.site_config.get("goto_timeout_ms", 30_000)),
                )
                items = await page.evaluate(_LIST_EXTRACT_JS, selector)
                self._emit_event(
                    make_event(
                        EXTRACT,
                        f"从列表页提取 {len(items or [])} 条链接",
                        level="info",
                        data={"count": len(items or [])},
                    )
                )
                for item in (items or [])[: self.max_items]:
                    url = item.get("url", "")
                    title = item.get("title", "")
                    if not url:
                        continue
                    if detail_re and not detail_re.search(url):
                        continue
                    notices.append(
                        BidNotice(
                            title=title,
                            url=url,
                            source_site_id=self.plan.site_id,
                            source_site_name=self.plan.site_name,
                            source_url=list_url,
                            page_type=PageType.DETAIL,
                        )
                    )
            except Exception as e:
                logger.warning("动态列表爬取失败: %s", e)
                return ScrapeResult(
                    site_id=self.plan.site_id,
                    success=False,
                    error=str(e),
                )

        if self.plan.list_strategy == ListStrategy.SPA and len(notices) < 3:
            self._emit_event(
                make_event(
                    RECURSE,
                    "SPA 站点条目较少，尝试智能爬取扩展",
                    level="warn",
                )
            )
            return await self._expand_intelligent(notices, mongo_repo)

        if self.site_config.get("enable_recursive") and notices:
            self._emit_event(
                make_event(RECURSE, "启用智能爬取扩展", level="info")
            )
            return await self._expand_intelligent(notices, mongo_repo)

        return ScrapeResult(
            site_id=self.plan.site_id,
            success=True,
            notices=notices,
        )

    async def _expand_intelligent(
        self,
        seed_notices: list[BidNotice],
        mongo_repo: Any,
    ) -> ScrapeResult:
        from src.intelligent_crawl.orchestrator import IntelligentCrawlOrchestrator

        adapter_name = self.site_config.get("adapter", "generic")
        if adapter_name == "generic":
            self._emit_event(
                make_event(
                    RECURSE,
                    "无适配器，跳过智能扩展，仅返回列表种子",
                    level="warn",
                )
            )
            return ScrapeResult(
                site_id=self.plan.site_id,
                success=True,
                notices=seed_notices,
            )

        schedule_defaults = get_schedule_defaults()
        adapter_cls = get_adapter_class(adapter_name)
        adapter = adapter_cls(self.site_config, self.browser)
        adapter.set_schedule_defaults(schedule_defaults)

        max_depth = self.plan.suggested_max_depth
        max_pages = min(self.max_items * 3, 30)

        orchestrator = IntelligentCrawlOrchestrator(
            browser=self.browser,
            site_config=self.site_config,
            adapter=adapter,
            max_depth=max_depth,
            max_pages=max_pages,
            min_delay=adapter._min_delay_seconds(),
            mongo_repo=mongo_repo,
            fetch_detail=should_fetch_detail(self.site_config),
        )
        all_notices = await orchestrator.run_from_seeds(seed_notices, max_pages=max_pages)
        self._emit_event(
            make_event(
                RECURSE,
                f"智能扩展完成: {len(seed_notices)} 种子 → {len(all_notices)} 条",
                level="success",
                data={"seed": len(seed_notices), "total": len(all_notices)},
            )
        )
        return ScrapeResult(
            site_id=self.plan.site_id,
            success=True,
            notices=all_notices,
        )
