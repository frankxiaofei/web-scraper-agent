"""每日商机线索同步：P0/P1 站点爬取 + 阶段词×业务词检索 + 分析触发。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.biz_clue_config import (
    get_biz_clue_search_keywords,
    get_biz_clue_sync_site_ids,
    load_biz_clue_sources,
)
from src.core.browser import BrowserPool
from src.core.pipeline import Pipeline
from src.core.site_sync import load_sites_config, sync_site
from src.core.sync_status import get_sync_status_store

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent


def load_biz_clue_sites(*, site_id: Optional[str] = None) -> list[dict[str, Any]]:
    """返回商机专用站点配置（P0/P1 列表，引用 sites.yaml enabled 站点）。"""
    config = load_sites_config()
    crawl_defaults = config.get("crawl_defaults", {})
    allowed = set(get_biz_clue_sync_site_ids())
    sources = load_biz_clue_sources()
    site_meta = {item.get("id"): item for item in (sources.get("sites") or []) if item.get("id")}

    if site_id:
        canonical = site_id.strip()
        allowed = {canonical} if canonical in allowed else set()

    sites: list[dict[str, Any]] = []
    for raw in config.get("sites", []):
        sid = raw.get("id")
        if sid not in allowed:
            continue
        if not raw.get("enabled"):
            continue
        meta = site_meta.get(sid) or {}
        merged = {**crawl_defaults, **raw}
        merged["biz_clue_display_name"] = meta.get("name") or raw.get("name") or sid
        merged["biz_clue_level"] = meta.get("level") or "P0"
        sites.append(merged)
    return sites


def _resolve_max_items(
    schedule_config: dict[str, Any],
    max_items: Optional[int],
) -> int:
    if max_items is not None:
        return max_items
    scheduler_cfg = schedule_config.get("scheduler", {})
    if scheduler_cfg.get("daily_biz_clue_sync_max_items") is not None:
        return int(scheduler_cfg["daily_biz_clue_sync_max_items"])
    sources = load_biz_clue_sources()
    return int(sources.get("default_max_items") or 30)


def _search_keywords_for_site(site_id: str, *, use_keyword_search: bool) -> list[Optional[str]]:
    if not use_keyword_search:
        return [None]
    sources = load_biz_clue_sources()
    limit = int(sources.get("max_search_keywords_per_site") or 0)
    if limit <= 0:
        return [None]
    kws = get_biz_clue_search_keywords()[:limit]
    return kws or [None]


async def run_daily_biz_clue_sync(
    *,
    schedule_config: dict[str, Any],
    max_items: Optional[int] = None,
    stagger: Optional[int] = None,
    site_id: Optional[str] = None,
    update_status: bool = True,
    use_keyword_search: bool = True,
    trigger_analysis: bool = True,
) -> dict[str, Any]:
    """按 bizclue 工作流执行商机爬取同步并触发分析。"""
    scheduler_cfg = schedule_config.get("scheduler", {})
    stagger_sec = stagger if stagger is not None else int(
        scheduler_cfg.get("daily_biz_clue_sync_stagger_seconds")
        or scheduler_cfg.get("daily_agri_sync_stagger_seconds", 30)
    )
    max_items_val = _resolve_max_items(schedule_config, max_items)
    cron = scheduler_cfg.get("daily_biz_clue_sync_cron", "30 6 * * *")

    sites = load_biz_clue_sites(site_id=site_id)
    if site_id and not sites:
        logger.error("未找到商机站点或未 enabled: %s", site_id)
        return {"success": False, "error": f"商机站点不存在或未启用: {site_id}"}

    if not sites:
        logger.error("没有可用的商机站点（检查 config/biz_clue_sources.yaml 与 sites.yaml）")
        return {"success": False, "error": "没有可用的商机站点"}

    status_store = get_sync_status_store()
    status_store.set_schedule_meta(cron=cron)

    pool = BrowserPool(headless=True)
    pipeline = Pipeline()
    await pool.start()

    site_stats: list[dict[str, Any]] = []
    ok, fail = 0, 0
    total_notices = 0
    total_biz_clue = 0

    try:
        for site in sites:
            sid = site.get("id", "")
            keywords = _search_keywords_for_site(sid, use_keyword_search=use_keyword_search)
            aggregate = {
                "site_id": sid,
                "site_name": site.get("biz_clue_display_name") or site.get("name") or sid,
                "source_level": site.get("biz_clue_level") or "P0",
                "success": True,
                "notices_count": 0,
                "new_count": 0,
                "biz_clue_count": 0,
                "duration_seconds": 0,
                "error": None,
                "search_runs": [],
            }

            for keyword in keywords:
                run_site = dict(site)
                if keyword:
                    run_site["biz_clue_search_keyword"] = keyword
                label = f"{sid}" + (f" [搜索:{keyword}]" if keyword else "")
                logger.info(
                    "商机同步站点: %s (adapter=%s, max_items=%d)",
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
                    trigger="daily_biz_clue_sync",
                )
                notices = int(outcome.get("notices_count") or 0)
                biz_count = int(outcome.get("biz_clue_count") or outcome.get("agri_count") or 0)
                aggregate["notices_count"] += notices
                aggregate["new_count"] += int(outcome.get("new_count") or 0)
                aggregate["biz_clue_count"] += biz_count
                aggregate["duration_seconds"] += int(outcome.get("duration_seconds") or 0)
                aggregate["search_runs"].append(
                    {
                        "keyword": keyword,
                        "success": bool(outcome.get("success")),
                        "notices_count": notices,
                        "biz_clue_count": biz_count,
                        "error": outcome.get("error"),
                    }
                )
                if not outcome.get("success"):
                    aggregate["success"] = False
                    aggregate["error"] = outcome.get("error")
                    logger.warning("站点 %s 失败: %s", label, outcome.get("error"))
                else:
                    logger.info(
                        "站点 %s 完成: 抓取 %d 条, 商机相关 %d 条",
                        label,
                        notices,
                        biz_count,
                    )
                if stagger_sec > 0 and (keyword != keywords[-1] or site != sites[-1]):
                    await asyncio.sleep(stagger_sec)

            site_stats.append(aggregate)
            total_notices += aggregate["notices_count"]
            total_biz_clue += aggregate["biz_clue_count"]
            if aggregate["success"]:
                ok += 1
            else:
                fail += 1
    finally:
        await pool.stop()

    analysis_summary: dict[str, Any] | None = None
    brief_summary: dict[str, Any] | None = None
    if trigger_analysis and scheduler_cfg.get("daily_biz_clue_sync_analysis_enabled", True):
        try:
            from src.web.biz_clue_service import get_biz_clue_service

            svc = get_biz_clue_service()
            analysis = svc.analyze_collected_data(days=30, refresh=True)
            analysis_summary = {
                "lead_count": len(analysis.get("leads") or []),
                "summary": analysis.get("summary"),
            }
            if scheduler_cfg.get("daily_biz_clue_brief_enabled", True):
                brief = svc.generate_daily_brief(days=30)
                brief_summary = {
                    "report_date": brief.get("report_date"),
                    "priority_count": brief.get("priority_count"),
                }
                cache_path = ROOT / "data" / "biz_clue_brief_cache.json"
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(brief, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception as exc:
            logger.warning("商机分析/简报生成失败: %s", exc)

    summary = {
        "success": fail == 0,
        "sites_ok": ok,
        "sites_fail": fail,
        "total_notices": total_notices,
        "total_biz_clue": total_biz_clue,
        "biz_clue_site_count": len(sites),
        "analysis": analysis_summary,
        "brief": brief_summary,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "sites": site_stats,
    }
    logger.info(
        "每日商机同步完成: 成功 %d, 失败 %d, 总抓取 %d, 商机相关 %d",
        ok,
        fail,
        total_notices,
        total_biz_clue,
    )
    return summary


def setup_daily_biz_clue_log_file(data_dir: Optional[Path] = None) -> Path:
    base = data_dir or (ROOT / "data")
    base.mkdir(parents=True, exist_ok=True)
    log_path = base / f"daily_biz_clue_{datetime.now().strftime('%Y%m%d')}.log"
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == str(log_path):
            return log_path
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    fh.setLevel(logging.INFO)
    root_logger.addHandler(fh)
    return log_path


__all__ = [
    "load_biz_clue_sites",
    "run_daily_biz_clue_sync",
    "setup_daily_biz_clue_log_file",
]
