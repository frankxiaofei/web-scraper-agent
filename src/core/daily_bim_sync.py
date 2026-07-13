"""每日 BIM 招标同步：全站 intelligent + with-detail 爬取并自动 BIM 分类。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.browser import BrowserPool
from src.core.bim_classifier import is_bim_classify_enabled
from src.core.pipeline import Pipeline
from src.core.site_sync import load_enabled_sites, load_sites_config, sync_site
from src.core.sync_status import get_sync_status_store

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent


def _bim_count_from_outcome(outcome: dict[str, Any]) -> int:
    return int(outcome.get("bim_count") or 0)


async def run_daily_bim_sync(
    *,
    schedule_config: dict[str, Any],
    max_items: Optional[int] = None,
    stagger: Optional[int] = None,
    site_id: Optional[str] = None,
    update_status: bool = True,
) -> dict[str, Any]:
    """串行同步所有 enabled 站点，汇总 BIM 统计。"""
    scheduler_cfg = schedule_config.get("scheduler", {})
    stagger_sec = stagger if stagger is not None else int(
        scheduler_cfg.get("daily_bim_sync_stagger_seconds")
        or scheduler_cfg.get("daily_sync_stagger_seconds", 30)
    )
    max_items_val = max_items if max_items is not None else int(
        scheduler_cfg.get("daily_bim_sync_max_items")
        or scheduler_cfg.get("daily_sync_max_items", 10)
    )
    cron = scheduler_cfg.get("daily_bim_sync_cron", "0 3 * * *")

    config = load_sites_config()
    crawl_defaults = config.get("crawl_defaults", {})
    sites = [{**crawl_defaults, **s} for s in load_enabled_sites()]
    if site_id:
        sites = [s for s in sites if s.get("id") == site_id]
        if not sites:
            logger.error("未找到 enabled 站点: %s", site_id)
            return {"success": False, "error": f"站点不存在或未启用: {site_id}"}

    if not sites:
        logger.error("没有 enabled 站点")
        return {"success": False, "error": "没有 enabled 站点"}

    status_store = get_sync_status_store()
    status_store.set_schedule_meta(cron=cron)

    pool = BrowserPool(headless=True)
    pipeline = Pipeline()
    await pool.start()

    site_stats: list[dict[str, Any]] = []
    ok, fail = 0, 0
    total_notices = 0
    total_bim = 0

    try:
        for site in sites:
            sid = site.get("id", "")
            logger.info("BIM 同步站点: %s (adapter=%s)", sid, site.get("adapter", "generic"))
            outcome = await sync_site(
                site,
                browser_pool=pool,
                pipeline=pipeline,
                max_items=max_items_val,
                update_status=update_status,
                trigger="daily_bim_sync",
            )
            notices = int(outcome.get("notices_count") or 0)
            bim_count = _bim_count_from_outcome(outcome)
            stat = {
                "site_id": sid,
                "site_name": site.get("name") or sid,
                "success": bool(outcome.get("success")),
                "notices_count": notices,
                "new_count": int(outcome.get("new_count") or 0),
                "bim_count": bim_count,
                "duration_seconds": int(outcome.get("duration_seconds") or 0),
                "error": outcome.get("error"),
            }
            site_stats.append(stat)
            total_notices += notices
            total_bim += bim_count

            if outcome.get("success"):
                ok += 1
                logger.info(
                    "站点 %s 完成: 抓取 %d 条, BIM 相关 %d 条",
                    sid,
                    notices,
                    bim_count,
                )
            else:
                fail += 1
                logger.warning("站点 %s 失败: %s", sid, outcome.get("error"))

            if stagger_sec > 0 and site != sites[-1]:
                await asyncio.sleep(stagger_sec)
    finally:
        await pool.stop()

    backfill_summary: dict[str, int] = {"attempted": 0, "updated": 0, "skipped": 0}
    content_backfill_summary: dict[str, int] = {"attempted": 0, "updated": 0, "skipped": 0}
    insights_summary: dict[str, Any] | None = None
    try:
        import sys

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts.backfill_bim_classify import run_backfill

        backfill_limit = int(
            scheduler_cfg.get("daily_bim_sync_backfill_limit", 200)
        )
        backfill_summary = run_backfill(source=site_id, limit=backfill_limit, force=False)
        logger.info(
            "BIM backfill: 处理 %d 条，更新 %d 条",
            backfill_summary.get("attempted", 0),
            backfill_summary.get("updated", 0),
        )
    except Exception as exc:
        logger.warning("BIM backfill 失败: %s", exc)

    if scheduler_cfg.get("daily_bim_sync_content_backfill_enabled", True):
        try:
            from scripts.backfill_bim_content import run_backfill as run_content_backfill

            content_limit = int(
                scheduler_cfg.get("daily_bim_sync_content_backfill_limit", 50)
            )
            content_backfill_summary = run_content_backfill(
                source=site_id,
                limit=content_limit,
                force=False,
            )
            logger.info(
                "BIM 正文补抓: 处理 %d 条，更新 %d 条",
                content_backfill_summary.get("attempted", 0),
                content_backfill_summary.get("updated", 0),
            )
        except Exception as exc:
            logger.warning("BIM 正文补抓失败: %s", exc)

    if scheduler_cfg.get("daily_bim_sync_insights_enabled", True):
        try:
            from src.web.bim_insights_service import DEFAULT_BIM_DAYS, get_bim_insights_service

            insights = get_bim_insights_service().get_insights(
                days=DEFAULT_BIM_DAYS, use_llm=True
            )
            insights_summary = {
                "bim_count_in_window": insights.get("bim_count_in_window"),
                "llm_summary": (insights.get("llm_analysis") or {}).get("summary"),
            }
        except Exception as exc:
            logger.warning("BIM 洞察生成失败: %s", exc)

    summary = {
        "success": fail == 0,
        "sites_ok": ok,
        "sites_fail": fail,
        "total_notices": total_notices,
        "total_bim": total_bim,
        "bim_classify_enabled": is_bim_classify_enabled(),
        "backfill": backfill_summary,
        "content_backfill": content_backfill_summary,
        "insights": insights_summary,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "sites": site_stats,
    }
    logger.info(
        "每日 BIM 同步完成: 成功 %d, 失败 %d, 总抓取 %d, BIM 相关 %d",
        ok,
        fail,
        total_notices,
        total_bim,
    )
    return summary


def setup_daily_bim_log_file(data_dir: Optional[Path] = None) -> Path:
    """创建 data/daily_bim_YYYYMMDD.log 并挂载 FileHandler。"""
    base = data_dir or (ROOT / "data")
    base.mkdir(parents=True, exist_ok=True)
    log_path = base / f"daily_bim_{datetime.now().strftime('%Y%m%d')}.log"
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == str(log_path):
            return log_path
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    fh.setLevel(logging.INFO)
    root_logger.addHandler(fh)
    return log_path
