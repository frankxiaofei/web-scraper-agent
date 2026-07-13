"""每日智慧农业招标同步：七大央企招采数据源 sync + 农业关键词筛选 + 洞察缓存。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.agri_classifier import is_agri_classify_enabled
from src.core.agri_sites import (
    apply_agri_entry_override,
    get_agri_search_keywords,
    load_agri_sites,
    load_agri_sites_config,
)
from src.core.browser import BrowserPool
from src.core.pipeline import Pipeline
from src.core.site_sync import load_sites_config, sync_site
from src.core.sync_status import get_sync_status_store

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent


def _agri_count_from_outcome(outcome: dict[str, Any]) -> int:
    return int(outcome.get("agri_count") or 0)


def _resolve_max_items(
    schedule_config: dict[str, Any],
    max_items: Optional[int],
) -> int:
    if max_items is not None:
        return max_items
    scheduler_cfg = schedule_config.get("scheduler", {})
    if scheduler_cfg.get("daily_agri_sync_max_items") is not None:
        return int(scheduler_cfg["daily_agri_sync_max_items"])
    agri_cfg = load_agri_sites_config()
    return int(agri_cfg.get("default_max_items") or scheduler_cfg.get("daily_sync_max_items", 10))


def _search_keywords_for_site(site_id: str, *, use_keyword_search: bool) -> list[Optional[str]]:
    if not use_keyword_search:
        return [None]
    agri_cfg = load_agri_sites_config()
    limit = int(agri_cfg.get("max_search_keywords_per_site") or 0)
    if limit <= 0:
        return [None]
    from src.core.agri_sites import resolve_agri_search_url

    kws = get_agri_search_keywords()[:limit]
    usable = [kw for kw in kws if resolve_agri_search_url(site_id, kw)]
    return usable or [None]


async def run_daily_agri_sync(
    *,
    schedule_config: dict[str, Any],
    max_items: Optional[int] = None,
    stagger: Optional[int] = None,
    site_id: Optional[str] = None,
    update_status: bool = True,
    use_keyword_search: bool = True,
) -> dict[str, Any]:
    """串行同步 agri 专用 7 站，汇总智慧农业统计并刷新洞察缓存。"""
    scheduler_cfg = schedule_config.get("scheduler", {})
    stagger_sec = stagger if stagger is not None else int(
        scheduler_cfg.get("daily_agri_sync_stagger_seconds")
        or scheduler_cfg.get("daily_sync_stagger_seconds", 30)
    )
    max_items_val = _resolve_max_items(schedule_config, max_items)
    cron = scheduler_cfg.get("daily_agri_sync_cron", "0 4 * * *")

    sites = load_agri_sites(site_id=site_id)
    if site_id and not sites:
        logger.error("未找到 agri 站点或未 enabled: %s", site_id)
        return {"success": False, "error": f"agri 站点不存在或未启用: {site_id}"}

    if not sites:
        logger.error("没有可用的 agri 站点（检查 config/agri_sites.yaml 与 sites.yaml enabled）")
        return {"success": False, "error": "没有可用的 agri 站点"}

    status_store = get_sync_status_store()
    status_store.set_schedule_meta(cron=cron)

    pool = BrowserPool(headless=True)
    pipeline = Pipeline()
    await pool.start()

    site_stats: list[dict[str, Any]] = []
    ok, fail = 0, 0
    total_notices = 0
    total_agri = 0

    try:
        for site in sites:
            sid = site.get("id", "")
            keywords = _search_keywords_for_site(sid, use_keyword_search=use_keyword_search)
            aggregate = {
                "site_id": sid,
                "site_name": site.get("agri_display_name") or site.get("name") or sid,
                "success": True,
                "notices_count": 0,
                "new_count": 0,
                "agri_count": 0,
                "duration_seconds": 0,
                "error": None,
                "search_runs": [],
            }

            for keyword in keywords:
                run_site = apply_agri_entry_override(site, keyword=keyword)
                label = f"{sid}" + (f" [搜索:{keyword}]" if keyword else "")
                logger.info(
                    "农业同步站点: %s (adapter=%s, max_items=%d)",
                    label,
                    run_site.get("adapter", "generic"),
                    max_items_val,
                )
                outcome = await sync_site(
                    run_site,
                    browser_pool=pool,
                    pipeline=pipeline,
                    max_items=max_items_val,
                    update_status=update_status and keyword is keywords[-1],
                    trigger="daily_agri_sync",
                )
                notices = int(outcome.get("notices_count") or 0)
                agri_count = _agri_count_from_outcome(outcome)
                aggregate["notices_count"] += notices
                aggregate["new_count"] += int(outcome.get("new_count") or 0)
                aggregate["agri_count"] += agri_count
                aggregate["duration_seconds"] += int(outcome.get("duration_seconds") or 0)
                aggregate["search_runs"].append(
                    {
                        "keyword": keyword,
                        "success": bool(outcome.get("success")),
                        "notices_count": notices,
                        "agri_count": agri_count,
                        "error": outcome.get("error"),
                    }
                )
                if not outcome.get("success"):
                    aggregate["success"] = False
                    aggregate["error"] = outcome.get("error")
                    logger.warning("站点 %s 失败: %s", label, outcome.get("error"))
                else:
                    logger.info(
                        "站点 %s 完成: 抓取 %d 条, 农业相关 %d 条",
                        label,
                        notices,
                        agri_count,
                    )
                if stagger_sec > 0 and (keyword != keywords[-1] or site != sites[-1]):
                    await asyncio.sleep(stagger_sec)

            site_stats.append(aggregate)
            total_notices += aggregate["notices_count"]
            total_agri += aggregate["agri_count"]
            if aggregate["success"]:
                ok += 1
            else:
                fail += 1
    finally:
        await pool.stop()

    backfill_summary: dict[str, int] = {"attempted": 0, "updated": 0, "skipped": 0}
    insights_summary: dict[str, Any] | None = None
    try:
        import sys

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts.backfill_agri_classify import run_backfill

        backfill_limit = int(scheduler_cfg.get("daily_agri_sync_backfill_limit", 200))
        backfill_summary = run_backfill(source=site_id, limit=backfill_limit, force=False)
        logger.info(
            "农业 backfill: 处理 %d 条，更新 %d 条",
            backfill_summary.get("attempted", 0),
            backfill_summary.get("updated", 0),
        )
    except Exception as exc:
        logger.warning("农业 backfill 失败: %s", exc)

    if scheduler_cfg.get("daily_agri_sync_insights_enabled", True):
        try:
            from src.web.agri_insights_service import (
                INSIGHTS_CACHE_PATH,
                get_agri_insights_service,
            )

            insights = get_agri_insights_service().get_insights(days=7)
            INSIGHTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            INSIGHTS_CACHE_PATH.write_text(
                json.dumps(
                    {
                        "cache_key": "7:all",
                        "generated_at": datetime.now().isoformat(),
                        "summary": insights,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            insights_summary = {
                "agri_count_in_window": insights.get("agri_count_in_window"),
            }
        except Exception as exc:
            logger.warning("农业洞察生成失败: %s", exc)

    summary = {
        "success": fail == 0,
        "sites_ok": ok,
        "sites_fail": fail,
        "total_notices": total_notices,
        "total_agri": total_agri,
        "agri_classify_enabled": is_agri_classify_enabled(),
        "agri_keyword_classify": True,
        "agri_site_count": len(sites),
        "backfill": backfill_summary,
        "insights": insights_summary,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "sites": site_stats,
    }
    logger.info(
        "每日农业同步完成: 成功 %d, 失败 %d, 总抓取 %d, 农业相关 %d",
        ok,
        fail,
        total_notices,
        total_agri,
    )
    return summary


def setup_daily_agri_log_file(data_dir: Optional[Path] = None) -> Path:
    base = data_dir or (ROOT / "data")
    base.mkdir(parents=True, exist_ok=True)
    log_path = base / f"daily_agri_{datetime.now().strftime('%Y%m%d')}.log"
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == str(log_path):
            return log_path
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    fh.setLevel(logging.INFO)
    root_logger.addHandler(fh)
    return log_path
