"""单站点同步执行：供调度器、每日任务与 Web API 触发共用。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.browser import BrowserPool
from src.core.crawl_links import notice_to_link
from src.core.config import get_settings
from src.core.site_aliases import resolve_canonical_site_id
from src.core.pipeline import Pipeline
from src.core.sync_cancel import SyncCancelledError
from src.core.sync_status import get_sync_status_store
from src.db.mongo_repository import create_mongo_repository

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
SITES_PATH = ROOT / "config" / "sites.yaml"
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"

DETAIL_ADAPTERS = frozenset({"ccgp", "ccgp_provincial", "ggzy", "ggzy_provincial"})

VALID_CRAWL_SCOPES = frozenset({"all", "soe_only", "soe_and_industry"})

CRAWL_SCOPE_LABELS: dict[str, str] = {
    "soe_only": "仅国央企",
    "soe_and_industry": "国央企 + 行业平台",
    "all": "全部 enabled 站",
}


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_sites_config() -> dict[str, Any]:
    return load_yaml(SITES_PATH)


def get_site_by_id(site_id: str) -> Optional[dict[str, Any]]:
    canonical_id = resolve_canonical_site_id(site_id)
    config = load_sites_config()
    crawl_defaults = config.get("crawl_defaults", {})
    for site in config.get("sites", []):
        if site.get("id") == canonical_id:
            return {**crawl_defaults, **site}
    return None


def get_crawl_scope() -> str:
    """读取 schedule.yaml 中的爬取范围：all | soe_only | soe_and_industry（默认 soe_only）。"""
    if not SCHEDULE_PATH.exists():
        return "soe_only"
    schedule_config = load_yaml(SCHEDULE_PATH)
    scope = (schedule_config.get("scheduler") or {}).get("crawl_scope", "soe_only")
    return scope if scope in VALID_CRAWL_SCOPES else "soe_only"


def get_crawl_scope_label(scope: str | None = None) -> str:
    """爬取范围的人类可读说明（供 Web UI / 文档引用）。"""
    effective = scope or get_crawl_scope()
    return CRAWL_SCOPE_LABELS.get(effective, effective)


def is_soe_site(site: dict[str, Any]) -> bool:
    """国央企站点：显式 soe 标记，或 enterprise 且 parent 非空。"""
    if "soe" in site:
        return bool(site.get("soe"))
    return site.get("category") == "enterprise" and bool(site.get("parent"))


def is_industry_site(site: dict[str, Any]) -> bool:
    """行业扩展站：sites.yaml 中 industry: true（如 dlzb 主站、省级 ggzy 试点）。"""
    return bool(site.get("industry"))


def site_in_crawl_scope(site: dict[str, Any], scope: str | None = None) -> bool:
    """站点是否落在当前 crawl_scope 内。"""
    effective = scope or get_crawl_scope()
    if effective == "all":
        return True
    if effective == "soe_only":
        return is_soe_site(site)
    if effective == "soe_and_industry":
        return is_soe_site(site) or is_industry_site(site)
    return False


def load_enabled_sites(*, respect_crawl_scope: bool = True) -> list[dict[str, Any]]:
    config = load_sites_config()
    crawl_defaults = config.get("crawl_defaults", {})
    sites = [
        {**crawl_defaults, **s}
        for s in config.get("sites", [])
        if s.get("enabled")
    ]
    if respect_crawl_scope:
        scope = get_crawl_scope()
        if scope != "all":
            sites = [s for s in sites if site_in_crawl_scope(s, scope)]
    return sites


def _generic_has_crawl_rules(site: dict[str, Any]) -> bool:
    """generic 站点是否已配置可用的声明式爬取规则。"""
    from src.core.crawl_rules import load_crawl_rule

    rule = load_crawl_rule(site)
    return bool(rule and rule.list_page)


def should_fetch_detail(site: dict[str, Any]) -> bool:
    if not site.get("fetch_detail"):
        return False
    adapter = site.get("adapter", "")
    if adapter == "wechat":
        return False
    if adapter in DETAIL_ADAPTERS:
        return True
    if adapter == "generic" and _generic_has_crawl_rules(site):
        from src.core.crawl_rules import load_crawl_rule

        rule = load_crawl_rule(site)
        return bool(rule and rule.detail and rule.detail.fetch_detail)
    return False


def get_schedule_defaults() -> dict[str, Any]:
    if not SCHEDULE_PATH.exists():
        return {"default_min_delay_seconds": 0, "max_retries": 3}
    schedule_config = load_yaml(SCHEDULE_PATH)
    scheduler_cfg = schedule_config.get("scheduler", {})
    return {
        "default_min_delay_seconds": scheduler_cfg.get("default_min_delay_seconds", 0),
        "max_retries": scheduler_cfg.get("max_retries", 3),
    }


def resolve_max_items(site: dict[str, Any], default: int = 10) -> int:
    """从 sites.yaml / crawl_rules 解析本次抓取条数上限。"""
    if "max_items" in site:
        return int(site["max_items"])
    from src.core.crawl_rules import load_crawl_rule

    rule = load_crawl_rule(site)
    if rule and rule.limits.max_items:
        return int(rule.limits.max_items)
    return default


async def sync_site(
    site: dict[str, Any],
    *,
    browser_pool: Optional[BrowserPool] = None,
    pipeline: Optional[Pipeline] = None,
    max_items: Optional[int] = None,
    update_status: bool = True,
    trigger: str = "manual",
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """抓取单个站点并更新同步状态。"""
    site_id = site["id"]
    site_name = site.get("name") or site_id
    site_url = site.get("url") or ""
    adapter_name = site.get("adapter", "generic")
    status_store = get_sync_status_store()
    effective_max_items = max_items if max_items is not None else resolve_max_items(site)

    from src.core.crawl_run import CrawlRunContext, RunTrigger, crawl_run_scope

    run_trigger: RunTrigger = trigger if trigger in (
        "scheduled", "manual", "manual_retry", "daily_sync", "daily_bim_sync", "daily_agri_sync", "daily_biz_clue_sync", "cli"
    ) else "manual"
    run_ctx = CrawlRunContext(
        site_id=site_id,
        site_name=site_name,
        site_url=site_url,
        trigger=run_trigger,
        run_id=run_id,
    )

    if adapter_name == "generic" and not _generic_has_crawl_rules(site):
        error = "尚未配置适配器"
        async with crawl_run_scope(run_ctx):
            run_ctx.finish_skipped(error)
        if update_status:
            status_store.mark_failed(site_id, error=error, duration_seconds=0)
        return {"site_id": site_id, "success": False, "error": error, "run_id": run_ctx.run_id}

    own_pool = browser_pool is None
    pool = browser_pool or BrowserPool(headless=True)
    pipe = pipeline or Pipeline(settings=get_settings())
    schedule_defaults = get_schedule_defaults()
    fetch_detail = should_fetch_detail(site)
    intelligent = bool(site.get("enable_recursive"))
    settings = get_settings()
    mongo_repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )

    if update_status:
        status_store.mark_syncing(site_id, site_name, site_url)

    started = time.monotonic()
    if own_pool:
        await pool.start()

    from src.core.site_credentials import reset_site_auth_context, set_site_auth_context

    auth_token = set_site_auth_context(site_id)
    async with crawl_run_scope(run_ctx):
        try:
            from src.core.scheduler import get_adapter_class

            adapter_cls = get_adapter_class(adapter_name)
            adapter = adapter_cls(site, pool)
            adapter.set_schedule_defaults(schedule_defaults)
            started_at = datetime.now(timezone.utc)
            run_ctx.emit(
                "fetch_list",
                f"开始抓取列表 (max_items={effective_max_items}, detail={fetch_detail}, intelligent={intelligent})",
                data={"url": site_url},
            )
            result = await adapter.run(
                max_items=effective_max_items,
                fetch_detail=True if fetch_detail else None,
                intelligent=True if intelligent else None,
                mongo_repo=mongo_repo,
            )
            elapsed = int(time.monotonic() - started)

            if not result.success:
                error = result.error or "unknown error"
                if error == "cancelled" or error == "用户已中止同步":
                    run_ctx.finish_cancelled(
                        duration_seconds=elapsed,
                        notices_count=len(result.notices),
                    )
                    if update_status:
                        status_store.mark_cancelled(
                            site_id,
                            duration_seconds=elapsed,
                            notices_count=len(result.notices),
                        )
                    return {
                        "site_id": site_id,
                        "success": False,
                        "cancelled": True,
                        "error": error,
                        "duration_seconds": elapsed,
                        "run_id": run_ctx.run_id,
                    }
                run_ctx.finish_failed(error, duration_seconds=elapsed)
                if update_status:
                    status_store.mark_failed(site_id, error=error, duration_seconds=elapsed)
                return {
                    "site_id": site_id,
                    "success": False,
                    "error": error,
                    "duration_seconds": elapsed,
                    "run_id": run_ctx.run_id,
                }

            notice_links = [notice_to_link(n) for n in result.notices]
            run_ctx.emit(
                "parse",
                f"解析完成: {len(result.notices)} 条公告",
                data={"count": len(result.notices), "urls": notice_links},
            )
            processed = pipe.process(result)
            run_ctx.emit(
                "save",
                f"入库: 新增 {len(processed.new)}, 更新 {len(processed.enriched)}",
                data={
                    "new": len(processed.new),
                    "enriched": len(processed.enriched),
                    "urls": notice_links,
                },
            )
            pipe.save(processed)
            pipe.log_scrape_job(
                result,
                new_count=len(processed.new) + len(processed.enriched),
                job_id=f"sync_{site_id}",
                started_at=started_at,
            )
            notices_count = len(result.notices)
            new_count = len(processed.new) + len(processed.enriched)
            bim_count = sum(
                1 for n in processed.all_for_storage if n.is_bim_related is True
            )
            agri_count = sum(
                1 for n in processed.all_for_storage if n.is_agri_related is True
            )
            run_ctx.finish_success(
                notices_count=notices_count,
                new_count=new_count,
                duration_seconds=elapsed,
            )
            if update_status:
                status_store.mark_completed(
                    site_id,
                    notices_count=notices_count,
                    new_count=new_count,
                    bim_count=bim_count,
                    agri_count=agri_count,
                    duration_seconds=elapsed,
                )
            return {
                "site_id": site_id,
                "success": True,
                "notices_count": notices_count,
                "new_count": new_count,
                "bim_count": bim_count,
                "agri_count": agri_count,
                "duration_seconds": elapsed,
                "run_id": run_ctx.run_id,
            }
        except SyncCancelledError as exc:
            elapsed = int(time.monotonic() - started)
            run_ctx.finish_cancelled(duration_seconds=elapsed)
            if update_status:
                status_store.mark_cancelled(site_id, duration_seconds=elapsed)
            return {
                "site_id": site_id,
                "success": False,
                "cancelled": True,
                "error": str(exc),
                "duration_seconds": elapsed,
                "run_id": run_ctx.run_id,
            }
        except Exception as exc:
            elapsed = int(time.monotonic() - started)
            logger.exception("站点 %s 同步异常", site_id)
            run_ctx.finish_failed(str(exc), duration_seconds=elapsed)
            if update_status:
                status_store.mark_failed(site_id, error=str(exc), duration_seconds=elapsed)
            return {
                "site_id": site_id,
                "success": False,
                "error": str(exc),
                "duration_seconds": elapsed,
                "run_id": run_ctx.run_id,
            }
        finally:
            reset_site_auth_context(auth_token)
            if mongo_repo:
                mongo_repo.close()
            if own_pool:
                await pool.stop()


async def sync_site_by_id(site_id: str, **kwargs: Any) -> dict[str, Any]:
    site = get_site_by_id(site_id)
    if not site:
        return {"site_id": site_id, "success": False, "error": "站点不存在"}
    if not site.get("enabled"):
        return {"site_id": site_id, "success": False, "error": "站点未启用"}
    if not site_in_crawl_scope(site):
        scope_label = get_crawl_scope_label()
        return {
            "site_id": site_id,
            "success": False,
            "error": f"当前爬取范围（{scope_label}）不包含该站点",
        }
    return await sync_site(site, **kwargs)
