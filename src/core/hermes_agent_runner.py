"""Hermes Agent Runner — 编排层，统一 scheduler / manual sync / BIM sync 入口。

当 ``HERMES_AGENT_URL`` 已配置且进程角色为 ``scheduler`` 时，经外部 hermes-agent
Gateway HTTP（crawl dispatch → crawl toolset → web_scraper API）触发任务；
否则在本进程 headless 直接 ``sync_site``（保留审计标记）。

环境变量 ``CRAWL_VIA_HERMES=false`` 时跳过 Gateway/审计，始终直连 ``sync_site``。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from src.core.browser import BrowserPool
from src.core.crawl_agent_actions import append_action
from src.core.config import get_settings
from src.core.hermes_gateway_client import (
    get_hermes_gateway_client,
    should_execute_sync_directly,
    should_use_hermes_gateway,
)
from src.core.pipeline import Pipeline
from src.core.schedule_eligibility import site_eligible_for_schedule
from src.core.site_sync import (
    get_site_by_id,
    load_enabled_sites,
    load_sites_config,
    resolve_max_items,
    sync_site,
    sync_site_by_id,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


class HermesTaskType:
    SITE_SYNC = "site_sync"
    DAILY_SYNC = "daily_sync"
    DAILY_BIM_SYNC = "daily_bim_sync"


def crawl_via_hermes_enabled() -> bool:
    """是否经 Hermes Agent Runner 编排（默认 true）。"""
    settings = get_settings()
    return bool(settings.crawl_via_hermes)


def _load_schedule_config() -> dict[str, Any]:
    if not SCHEDULE_PATH.exists():
        return {}
    import yaml

    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def _audit_hermes_action(
    site_id: str,
    task_type: str,
    trigger: str,
    detail: str,
) -> None:
    try:
        append_action(
            site_id=site_id,
            action=f"hermes_{task_type}",
            detail=f"trigger={trigger}; {detail}",
            source="hermes",
        )
    except Exception:
        logger.debug("hermes audit append failed", exc_info=True)


async def run_hermes_crawl_task(
    task_type: str,
    *,
    site_id: Optional[str] = None,
    trigger: str = "manual",
    params: Optional[dict[str, Any]] = None,
    browser_pool: Optional[BrowserPool] = None,
    pipeline: Optional[Pipeline] = None,
    run_id: Optional[str] = None,
    site: Optional[dict[str, Any]] = None,
    update_status: bool = True,
) -> dict[str, Any]:
    """
    Hermes Agent 统一爬取入口。

    task_type: site_sync | daily_sync | daily_bim_sync
    """
    params = params or {}
    via_hermes = crawl_via_hermes_enabled()
    use_gateway = should_use_hermes_gateway()

    if task_type == HermesTaskType.SITE_SYNC:
        return await _run_site_sync(
            site_id=site_id,
            site=site,
            trigger=trigger,
            run_id=run_id,
            browser_pool=browser_pool,
            pipeline=pipeline,
            update_status=update_status,
            max_items=params.get("max_items"),
            via_hermes=via_hermes,
            use_gateway=use_gateway,
        )

    if task_type == HermesTaskType.DAILY_SYNC:
        return await _run_daily_sync(
            trigger=trigger or "daily_sync",
            browser_pool=browser_pool,
            pipeline=pipeline,
            update_status=update_status,
            stagger=int(params.get("stagger", 30)),
            max_items=int(params.get("max_items", 10)),
            via_hermes=via_hermes,
            use_gateway=use_gateway,
        )

    if task_type == HermesTaskType.DAILY_BIM_SYNC:
        return await _run_daily_bim_sync(
            site_id=site_id,
            trigger=trigger or "daily_bim_sync",
            update_status=update_status,
            max_items=params.get("max_items"),
            stagger=params.get("stagger"),
            via_hermes=via_hermes,
            use_gateway=use_gateway,
        )

    return {"ok": False, "success": False, "error": f"未知 task_type: {task_type}"}


async def _run_site_sync_via_gateway(
    *,
    site_id: str,
    trigger: str,
    max_items: Optional[int],
    via_hermes: bool,
) -> dict[str, Any]:
    if via_hermes:
        _audit_hermes_action(
            site_id,
            HermesTaskType.SITE_SYNC,
            trigger,
            f"gateway max_items={max_items}; path=hermes-agent",
        )
        logger.info(
            "Hermes Gateway: site_sync %s trigger=%s max_items=%s",
            site_id,
            trigger,
            max_items,
        )

    client = get_hermes_gateway_client()
    outcome = await client.run_site_sync(
        site_id,
        trigger=trigger,
        max_items=max_items,
        wait=True,
    )
    if via_hermes:
        outcome = {
            **outcome,
            "hermes_runner": True,
            "task_type": HermesTaskType.SITE_SYNC,
        }
    return outcome


async def _run_site_sync_direct(
    *,
    site_id: Optional[str],
    site: Optional[dict[str, Any]],
    trigger: str,
    run_id: Optional[str],
    browser_pool: Optional[BrowserPool],
    pipeline: Optional[Pipeline],
    update_status: bool,
    max_items: Optional[int],
    via_hermes: bool,
) -> dict[str, Any]:
    merged_site: Optional[dict[str, Any]] = site
    if merged_site is None and site_id:
        raw = get_site_by_id(site_id)
        if not raw:
            return {"site_id": site_id, "success": False, "error": "站点不存在"}
        config = load_sites_config()
        crawl_defaults = config.get("crawl_defaults", {})
        merged_site = {**crawl_defaults, **raw}

    if not merged_site:
        return {"success": False, "error": "site_id 或 site 必填"}

    sid = merged_site.get("id") or site_id or ""
    effective_max = max_items if max_items is not None else resolve_max_items(merged_site)

    if via_hermes:
        _audit_hermes_action(
            sid,
            HermesTaskType.SITE_SYNC,
            trigger,
            f"max_items={effective_max}",
        )
        logger.info("Hermes Runner: site_sync %s trigger=%s", sid, trigger)

    if site_id and not site:
        outcome = await sync_site_by_id(
            site_id,
            browser_pool=browser_pool,
            pipeline=pipeline,
            max_items=effective_max,
            update_status=update_status,
            trigger=trigger,
            run_id=run_id,
        )
    else:
        outcome = await sync_site(
            merged_site,
            browser_pool=browser_pool,
            pipeline=pipeline,
            max_items=effective_max,
            update_status=update_status,
            trigger=trigger,
            run_id=run_id,
        )

    if via_hermes:
        outcome = {**outcome, "hermes_runner": True, "task_type": HermesTaskType.SITE_SYNC}
    return outcome


async def _run_site_sync(
    *,
    site_id: Optional[str],
    site: Optional[dict[str, Any]],
    trigger: str,
    run_id: Optional[str],
    browser_pool: Optional[BrowserPool],
    pipeline: Optional[Pipeline],
    update_status: bool,
    max_items: Optional[int],
    via_hermes: bool,
    use_gateway: bool,
) -> dict[str, Any]:
    resolved_site_id = site_id or (site or {}).get("id")
    if use_gateway and resolved_site_id:
        return await _run_site_sync_via_gateway(
            site_id=str(resolved_site_id),
            trigger=trigger,
            max_items=max_items,
            via_hermes=via_hermes,
        )

    if not should_execute_sync_directly() and via_hermes and not use_gateway:
        return {
            "site_id": resolved_site_id,
            "success": False,
            "error": "HERMES_AGENT_URL 未配置，scheduler 无法委托 Gateway",
        }

    return await _run_site_sync_direct(
        site_id=site_id,
        site=site,
        trigger=trigger,
        run_id=run_id,
        browser_pool=browser_pool,
        pipeline=pipeline,
        update_status=update_status,
        max_items=max_items,
        via_hermes=via_hermes,
    )


async def _run_daily_sync(
    *,
    trigger: str,
    browser_pool: Optional[BrowserPool],
    pipeline: Optional[Pipeline],
    update_status: bool,
    stagger: int,
    max_items: int,
    via_hermes: bool,
    use_gateway: bool,
) -> dict[str, Any]:
    if via_hermes:
        _audit_hermes_action(
            "_all",
            HermesTaskType.DAILY_SYNC,
            trigger,
            f"stagger={stagger} max_items={max_items}"
            + ("; path=hermes-agent" if use_gateway else ""),
        )
        logger.info(
            "Hermes %s: daily_sync stagger=%ds max_items=%d",
            "Gateway" if use_gateway else "Runner",
            stagger,
            max_items,
        )

    if use_gateway and not should_execute_sync_directly():
        client = get_hermes_gateway_client()
        sites_ok, sites_fail = 0, 0
        for site in load_enabled_sites():
            site_id = site.get("id", "")
            if not site_eligible_for_schedule(site):
                continue
            outcome = await client.run_site_sync(
                site_id,
                trigger=trigger,
                max_items=max_items,
                wait=True,
            )
            if outcome.get("success"):
                sites_ok += 1
            else:
                sites_fail += 1
            if stagger > 0:
                await asyncio.sleep(stagger)
        return {
            "success": sites_fail == 0,
            "sites_ok": sites_ok,
            "sites_fail": sites_fail,
            "task_type": HermesTaskType.DAILY_SYNC,
            "hermes_runner": via_hermes,
            "hermes_gateway": True,
        }

    from src.core.sync_status import get_sync_status_store

    sync_status = get_sync_status_store()
    sites_ok, sites_fail = 0, 0

    for site in load_enabled_sites():
        site_id = site.get("id", "")
        if not site_eligible_for_schedule(site):
            if site.get("adapter") == "generic":
                sync_status.ensure_site(site_id, site.get("name") or site_id, site.get("url") or "")
            continue

        outcome = await run_hermes_crawl_task(
            HermesTaskType.SITE_SYNC,
            site=site,
            trigger=trigger,
            browser_pool=browser_pool,
            pipeline=pipeline,
            update_status=update_status,
            params={"max_items": max_items},
        )
        if outcome.get("success"):
            sites_ok += 1
        else:
            sites_fail += 1
        if stagger > 0:
            await asyncio.sleep(stagger)

    summary = {
        "success": sites_fail == 0,
        "sites_ok": sites_ok,
        "sites_fail": sites_fail,
        "task_type": HermesTaskType.DAILY_SYNC,
        "hermes_runner": via_hermes,
    }
    return summary


async def _run_daily_bim_sync(
    *,
    site_id: Optional[str],
    trigger: str,
    update_status: bool,
    max_items: Optional[int],
    stagger: Optional[int],
    via_hermes: bool,
    use_gateway: bool,
) -> dict[str, Any]:
    schedule_config = _load_schedule_config()
    detail_parts = [f"site_id={site_id or 'all'}"]
    if max_items is not None:
        detail_parts.append(f"max_items={max_items}")
    if via_hermes:
        _audit_hermes_action(
            site_id or "_bim_all",
            HermesTaskType.DAILY_BIM_SYNC,
            trigger,
            "; ".join(detail_parts) + ("; path=hermes-agent" if use_gateway else ""),
        )
        logger.info(
            "Hermes %s: daily_bim_sync %s",
            "Gateway" if use_gateway else "Runner",
            "; ".join(detail_parts),
        )

    if use_gateway and not should_execute_sync_directly():
        summary = await get_hermes_gateway_client().run_daily_bim_sync(
            site_id=site_id,
            max_items=max_items,
            stagger=stagger,
        )
        if via_hermes:
            summary = {
                **summary,
                "hermes_runner": True,
                "task_type": HermesTaskType.DAILY_BIM_SYNC,
            }
        return summary

    from src.core.daily_bim_sync import run_daily_bim_sync

    summary = await run_daily_bim_sync(
        schedule_config=schedule_config,
        site_id=site_id,
        max_items=max_items,
        stagger=stagger,
        update_status=update_status,
    )
    if via_hermes:
        summary = {
            **summary,
            "hermes_runner": True,
            "task_type": HermesTaskType.DAILY_BIM_SYNC,
        }
    return summary


async def run_scheduler_site_sync(
    site_id: str,
    *,
    trigger: str = "scheduled",
    browser_pool: Optional[BrowserPool] = None,
    pipeline: Optional[Pipeline] = None,
    max_items: Optional[int] = None,
) -> dict[str, Any]:
    """Scheduler per-site 任务入口。"""
    config = load_sites_config()
    crawl_defaults = config.get("crawl_defaults", {})
    raw = get_site_by_id(site_id)
    if not raw:
        return {"site_id": site_id, "success": False, "error": "站点不存在"}
    merged_site = {**crawl_defaults, **raw}
    effective_max = max_items if max_items is not None else resolve_max_items(merged_site)
    return await run_hermes_crawl_task(
        HermesTaskType.SITE_SYNC,
        site_id=site_id,
        site=merged_site,
        trigger=trigger,
        browser_pool=browser_pool,
        pipeline=pipeline,
        update_status=True,
        params={"max_items": effective_max},
    )


async def run_scheduler_daily_sync(
    *,
    browser_pool: Optional[BrowserPool] = None,
    pipeline: Optional[Pipeline] = None,
    stagger: int = 30,
    max_items: int = 10,
) -> dict[str, Any]:
    """Scheduler daily_sync cron 入口。"""
    return await run_hermes_crawl_task(
        HermesTaskType.DAILY_SYNC,
        trigger="daily_sync",
        browser_pool=browser_pool,
        pipeline=pipeline,
        update_status=True,
        params={"stagger": stagger, "max_items": max_items},
    )


async def run_scheduler_daily_bim_sync(
    *,
    schedule_config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Scheduler daily_bim_sync cron 入口。"""
    cfg = schedule_config or _load_schedule_config()
    scheduler_cfg = cfg.get("scheduler", {})
    return await run_hermes_crawl_task(
        HermesTaskType.DAILY_BIM_SYNC,
        trigger="daily_bim_sync",
        update_status=True,
        params={
            "max_items": scheduler_cfg.get("daily_bim_sync_max_items")
            or scheduler_cfg.get("daily_sync_max_items", 10),
            "stagger": scheduler_cfg.get("daily_bim_sync_stagger_seconds")
            or scheduler_cfg.get("daily_sync_stagger_seconds", 30),
        },
    )
