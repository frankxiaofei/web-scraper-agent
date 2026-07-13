"""APScheduler 定时任务调度。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.adapters.base import BaseAdapter
from src.adapters.cebpubservice import CebpubserviceAdapter
from src.adapters.bidchance import BidchanceAdapter
from src.adapters.cecbid import CecbidAdapter
from src.adapters.china_tender import ChinaTenderAdapter
from src.adapters.chinazbbid import ChinazbbidAdapter
from src.adapters.chinamobile import ChinaMobileAdapter
from src.adapters.crec import CrecAdapter
from src.adapters.generic import GenericRuleAdapter
from src.adapters.ccgp import CcgpNationalAdapter
from src.adapters.ccgp_provincial import CcgpProvincialAdapter
from src.adapters.csg import CsgBiddingAdapter
from src.adapters.ggzy import GgzyNationalAdapter
from src.adapters.ggzy_provincial import GgzyProvincialAdapter
from src.adapters.sgcc_ecp import SgccEcpAdapter
from src.adapters.sinopec import SinopecAdapter
from src.adapters.wechat import WechatAdapter
from src.adapters.zycg import ZycgAdapter
from src.core.alert import get_alert_manager
from src.core.browser import BrowserPool
from src.core.models import ScrapeResult
from src.core.pipeline import Pipeline
from src.core.schedule_eligibility import (
    filter_schedule_eligible_sites,
    site_eligible_for_schedule,
)
from src.core.site_sync import SITES_PATH, load_sites_config
from src.core.sync_status import get_sync_status_store

logger = logging.getLogger(__name__)

# 适配器注册表：adapter 名称 -> 适配器类
ADAPTER_REGISTRY: dict[str, type[BaseAdapter]] = {
    "ccgp": CcgpNationalAdapter,
    "ccgp_provincial": CcgpProvincialAdapter,
    "zycg": ZycgAdapter,
    "ggzy": GgzyNationalAdapter,
    "ggzy_provincial": GgzyProvincialAdapter,
    "cebpubservice": CebpubserviceAdapter,
    "cecbid": CecbidAdapter,
    "china_tender": ChinaTenderAdapter,
    "bidchance": BidchanceAdapter,
    "chinazbbid": ChinazbbidAdapter,
    "sgcc_ecp": SgccEcpAdapter,
    "csg": CsgBiddingAdapter,
    "sinopec": SinopecAdapter,
    "chinamobile": ChinaMobileAdapter,
    "crec": CrecAdapter,
    "wechat": WechatAdapter,
    "generic": GenericRuleAdapter,
}


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_adapter_class(adapter_name: str) -> type[BaseAdapter]:
    if adapter_name not in ADAPTER_REGISTRY:
        raise ValueError(f"未知适配器: {adapter_name}")
    return ADAPTER_REGISTRY[adapter_name]


class ScraperScheduler:
    """封装 APScheduler，按配置调度各站点抓取任务。"""

    def __init__(
        self,
        sites_config: dict[str, Any],
        schedule_config: dict[str, Any],
        browser_pool: BrowserPool,
        pipeline: Pipeline,
    ) -> None:
        self.sites_config = sites_config
        self.schedule_config = schedule_config
        self.browser_pool = browser_pool
        self.pipeline = pipeline
        self.scheduler = AsyncIOScheduler(
            timezone=schedule_config.get("scheduler", {}).get("timezone", "Asia/Shanghai"),
        )
        self._sites_by_id = {s["id"]: s for s in sites_config.get("sites", [])}
        self._schedule_defaults = self._build_schedule_defaults()
        self._consecutive_failures: dict[str, int] = {}
        self._alert = get_alert_manager()
        self._sync_status = get_sync_status_store()
        self._daily_sync_running = False
        self._daily_bim_sync_running = False
        self._daily_biz_clue_sync_running = False
        self._bim_backfill_running = False
        self._bim_brief_running = False
        self._bim_weekly_brief_running = False
        self._site_running: set[str] = set()
        self._script_cron_mtime: float = 0.0
        self._chat_scheduled_mtime: float = 0.0
        self._sites_yaml_mtime: float = (
            SITES_PATH.stat().st_mtime if SITES_PATH.exists() else 0.0
        )
        self._chat_scheduled_running: set[str] = set()

    def _build_schedule_defaults(self) -> dict[str, Any]:
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        return {
            "default_min_delay_seconds": scheduler_cfg.get("default_min_delay_seconds", 0),
            "max_retries": scheduler_cfg.get("max_retries", 3),
        }

    async def _run_site(self, site_id: str, *, trigger: str = "scheduled") -> None:
        site = self._sites_by_id.get(site_id)
        if not site:
            logger.error("未找到站点配置: %s", site_id)
            return
        if not site.get("enabled", False):
            logger.info("站点 %s 未启用，跳过", site_id)
            return
        if site_id in self._site_running:
            logger.info("站点 %s 正在运行，跳过本次调度", site_id)
            from src.core.crawl_run import CrawlRunContext
            from src.core.crawl_run_store import get_crawl_run_store

            run_id = f"skip_{site_id}_{int(datetime.now(timezone.utc).timestamp())}"
            store = get_crawl_run_store()
            store.create_run(
                run_id=run_id,
                site_id=site_id,
                site_name=site.get("name") or site_id,
                trigger=trigger,  # type: ignore[arg-type]
            )
            store.finish_run(run_id, status="skipped", error="站点正在运行中")
            return

        from src.core.hermes_agent_runner import run_scheduler_site_sync

        self._site_running.add(site_id)
        try:
            outcome = await run_scheduler_site_sync(
                site_id,
                trigger=trigger,
                browser_pool=self.browser_pool,
                pipeline=self.pipeline,
            )
            if not outcome.get("success"):
                result = ScrapeResult(
                    site_id=site_id,
                    success=False,
                    error=outcome.get("error"),
                )
                self._handle_failure_alert(site_id, result)
            else:
                self._consecutive_failures[site_id] = 0
        finally:
            self._site_running.discard(site_id)

    async def _run_daily_sync(self) -> None:
        if self._daily_sync_running:
            logger.warning("每日全站同步已在运行，跳过本次触发")
            return
        self._daily_sync_running = True
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        stagger = int(scheduler_cfg.get("daily_sync_stagger_seconds", 30))
        max_items = int(scheduler_cfg.get("daily_sync_max_items", 10))
        from src.core.hermes_agent_runner import run_scheduler_daily_sync

        logger.info("开始每日全站同步（Hermes Runner，错峰 %ds）", stagger)
        try:
            await run_scheduler_daily_sync(
                browser_pool=self.browser_pool,
                pipeline=self.pipeline,
                stagger=stagger,
                max_items=max_items,
            )
        finally:
            self._daily_sync_running = False
            logger.info("每日全站同步结束")

    async def _run_daily_bim_sync(self) -> None:
        if self._daily_bim_sync_running:
            logger.warning("每日 BIM 全站同步已在运行，跳过本次触发")
            return
        if self._daily_sync_running:
            logger.warning("每日全站同步正在运行，跳过 BIM 同步")
            return
        self._daily_bim_sync_running = True
        from src.core.daily_bim_sync import setup_daily_bim_log_file
        from src.core.hermes_agent_runner import run_scheduler_daily_bim_sync

        log_path = setup_daily_bim_log_file()
        logger.info("开始每日 BIM 全站同步（Hermes Runner），日志: %s", log_path)
        try:
            summary = await run_scheduler_daily_bim_sync(
                schedule_config=self.schedule_config,
            )
            logger.info(
                "每日 BIM 同步结束: 成功 %d, 失败 %d, BIM 相关 %d",
                summary.get("sites_ok", 0),
                summary.get("sites_fail", 0),
                summary.get("total_bim", 0),
            )
            scheduler_cfg = self.schedule_config.get("scheduler", {})
            if scheduler_cfg.get("bim_brief_push_after_sync", False) and self._is_bim_brief_enabled():
                await self._run_bim_daily_brief()
        finally:
            self._daily_bim_sync_running = False

    async def _run_daily_biz_clue_sync(self) -> None:
        if self._daily_biz_clue_sync_running:
            logger.warning("每日商机同步已在运行，跳过本次触发")
            return
        self._daily_biz_clue_sync_running = True
        from src.core.biz_clue_sync import run_daily_biz_clue_sync, setup_daily_biz_clue_log_file

        log_path = setup_daily_biz_clue_log_file()
        logger.info("开始每日商机线索同步，日志: %s", log_path)
        try:
            summary = await run_daily_biz_clue_sync(
                schedule_config=self.schedule_config,
                update_status=True,
                trigger_analysis=True,
            )
            logger.info(
                "每日商机同步结束: 成功 %d, 失败 %d, 商机相关 %d",
                summary.get("sites_ok", 0),
                summary.get("sites_fail", 0),
                summary.get("total_biz_clue", 0),
            )
        except Exception as exc:
            logger.exception("每日商机同步异常: %s", exc)
        finally:
            self._daily_biz_clue_sync_running = False

    async def _run_bim_backfill_labels(self) -> None:
        if self._bim_backfill_running:
            logger.warning("BIM 打标补全已在运行，跳过本次触发")
            return
        if self._daily_bim_sync_running:
            logger.warning("每日 BIM 同步正在运行，跳过打标补全")
            return
        self._bim_backfill_running = True
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        limit = int(scheduler_cfg.get("bim_backfill_labels_limit", 500))
        logger.info("开始 BIM 打标补全，limit=%d", limit)
        try:
            import sys

            root = Path(__file__).resolve().parent.parent.parent
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from scripts.backfill_bim_classify import run_backfill

            summary = run_backfill(source=None, limit=limit, force=False, dry_run=False)
            logger.info(
                "BIM 打标补全结束: 处理 %d 条，更新 %d 条，待打标剩余 %s",
                summary.get("attempted", 0),
                summary.get("updated", 0),
                (summary.get("after") or {}).get("bim_unclassified", "—"),
            )
        except Exception as exc:
            logger.warning("BIM 打标补全失败: %s", exc)
        finally:
            self._bim_backfill_running = False

    def _handle_failure_alert(self, site_id: str, result: ScrapeResult) -> None:
        if result.success:
            self._consecutive_failures[site_id] = 0
            return
        count = self._consecutive_failures.get(site_id, 0) + 1
        self._consecutive_failures[site_id] = count
        threshold = int(
            self.schedule_config.get("scheduler", {}).get("alert_consecutive_failures", 3)
        )
        if count >= threshold:
            self._alert.consecutive_failure(site_id, count, result.error)

    def _job_overrides(self) -> dict[str, dict[str, Any]]:
        overrides: dict[str, dict[str, Any]] = {}
        for job in self.schedule_config.get("jobs", []):
            site_id = job.get("site_id")
            if site_id:
                overrides[site_id] = job
        return overrides

    def _register_per_site_staggered_jobs(self) -> None:
        """为每个 enabled 站点注册独立 interval 任务，按 index×10min 错开首次触发。"""
        from src.core.site_sync import load_enabled_sites

        scheduler_cfg = self.schedule_config.get("scheduler", {})
        stagger_min = int(scheduler_cfg.get("round_robin_interval_minutes", 10))
        overrides = self._job_overrides()

        sites = filter_schedule_eligible_sites(
            load_enabled_sites(),
            job_overrides=overrides,
        )

        cycle_min = scheduler_cfg.get("per_site_cycle_minutes")
        if not cycle_min:
            cycle_min = max(len(sites), 1) * stagger_min
        else:
            cycle_min = int(cycle_min)

        now = datetime.now(self.scheduler.timezone)
        for i, site in enumerate(sites):
            site_id = site["id"]
            job_override = overrides.get(site_id, {})
            minutes = int(job_override.get("minutes", cycle_min))
            start_date = now + timedelta(minutes=i * stagger_min)

            trigger = IntervalTrigger(minutes=minutes, start_date=start_date)
            self.scheduler.add_job(
                self._run_site,
                trigger=trigger,
                kwargs={"site_id": site_id, "trigger": "scheduled"},
                id=f"scrape_{site_id}",
                replace_existing=True,
                max_instances=1,
            )
            logger.info(
                "已注册站点任务: %s, 周期 %d 分钟, 首次触发 +%d 分钟",
                site_id,
                minutes,
                i * stagger_min,
            )

    def register_jobs(self) -> None:
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        if scheduler_cfg.get("per_site_schedule_enabled", True):
            self._register_per_site_staggered_jobs()
            return

        jobs = self.schedule_config.get("jobs", [])
        default_minutes = scheduler_cfg.get("default_interval_minutes", 60)

        for job in jobs:
            if not job.get("enabled", False):
                logger.info("任务 %s 已禁用", job.get("site_id"))
                continue

            site_id = job["site_id"]
            minutes = job.get("minutes", default_minutes)
            trigger = IntervalTrigger(minutes=minutes)

            self.scheduler.add_job(
                self._run_site,
                trigger=trigger,
                kwargs={"site_id": site_id, "trigger": "scheduled"},
                id=f"scrape_{site_id}",
                replace_existing=True,
                max_instances=1,
            )
            logger.info("已注册任务: %s, 间隔 %d 分钟", site_id, minutes)

    def _register_daily_sync_job(self) -> None:
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        if not scheduler_cfg.get("daily_sync_enabled", True):
            logger.info("每日全站同步已禁用")
            return

        cron_expr = scheduler_cfg.get("daily_sync_cron", "0 2 * * *")
        parts = cron_expr.split()
        if len(parts) != 5:
            logger.error("daily_sync_cron 格式无效: %s", cron_expr)
            return

        minute, hour, day, month, day_of_week = parts
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=self.scheduler.timezone,
        )
        self.scheduler.add_job(
            self._run_daily_sync,
            trigger=trigger,
            id="daily_sync_all_sites",
            replace_existing=True,
            max_instances=1,
        )
        job = self.scheduler.get_job("daily_sync_all_sites")
        next_run = job.next_run_time if job else None
        self._sync_status.set_schedule_meta(cron=cron_expr, next_run=next_run)
        logger.info("已注册每日全站同步: cron=%s, 下次=%s", cron_expr, next_run)

    def _register_daily_bim_sync_job(self) -> None:
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        if not scheduler_cfg.get("daily_bim_sync_enabled", True):
            logger.info("每日 BIM 全站同步已禁用")
            return

        cron_expr = scheduler_cfg.get("daily_bim_sync_cron", "0 3 * * *")
        parts = cron_expr.split()
        if len(parts) != 5:
            logger.error("daily_bim_sync_cron 格式无效: %s", cron_expr)
            return

        minute, hour, day, month, day_of_week = parts
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=self.scheduler.timezone,
        )
        self.scheduler.add_job(
            self._run_daily_bim_sync,
            trigger=trigger,
            id="daily_bim_sync_all_sites",
            replace_existing=True,
            max_instances=1,
        )
        job = self.scheduler.get_job("daily_bim_sync_all_sites")
        next_run = job.next_run_time if job else None
        logger.info("已注册每日 BIM 全站同步: cron=%s, 下次=%s", cron_expr, next_run)

    def _register_daily_biz_clue_sync_job(self) -> None:
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        if not scheduler_cfg.get("daily_biz_clue_sync_enabled", True):
            logger.info("每日商机线索同步已禁用")
            return

        cron_expr = scheduler_cfg.get("daily_biz_clue_sync_cron", "30 6 * * *")
        parts = cron_expr.split()
        if len(parts) != 5:
            logger.error("daily_biz_clue_sync_cron 格式无效: %s", cron_expr)
            return

        minute, hour, day, month, day_of_week = parts
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=self.scheduler.timezone,
        )
        self.scheduler.add_job(
            self._run_daily_biz_clue_sync,
            trigger=trigger,
            id="daily_biz_clue_sync",
            replace_existing=True,
            max_instances=1,
        )
        job = self.scheduler.get_job("daily_biz_clue_sync")
        next_run = job.next_run_time if job else None
        logger.info("已注册每日商机线索同步: cron=%s, 下次=%s", cron_expr, next_run)

    def _register_bim_backfill_labels_job(self) -> None:
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        if not scheduler_cfg.get("bim_backfill_labels_enabled", True):
            logger.info("BIM 打标补全任务已禁用")
            return

        cron_expr = scheduler_cfg.get("bim_backfill_labels_cron", "30 3 * * *")
        parts = cron_expr.split()
        if len(parts) != 5:
            logger.error("bim_backfill_labels_cron 格式无效: %s", cron_expr)
            return

        minute, hour, day, month, day_of_week = parts
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=self.scheduler.timezone,
        )
        self.scheduler.add_job(
            self._run_bim_backfill_labels,
            trigger=trigger,
            id="bim_backfill_labels",
            replace_existing=True,
            max_instances=1,
        )
        job = self.scheduler.get_job("bim_backfill_labels")
        next_run = job.next_run_time if job else None
        logger.info("已注册 BIM 打标补全: cron=%s, 下次=%s", cron_expr, next_run)

    def _is_bim_brief_enabled(self) -> bool:
        from src.core.config import get_settings

        settings = get_settings()
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        if not settings.bim_brief_enabled:
            return False
        return scheduler_cfg.get("bim_brief_enabled", True)

    def _bim_brief_cron_expr(self) -> str:
        import os

        from src.core.config import get_settings

        settings = get_settings()
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        return (
            os.environ.get("BIM_BRIEF_CRON")
            or settings.bim_brief_cron
            or scheduler_cfg.get("bim_brief_cron")
            or "0 8 * * 0-4"
        )

    async def _run_bim_daily_brief(self) -> None:
        if self._bim_brief_running:
            logger.info("BIM 日报推送已在运行，跳过")
            return
        if not self._is_bim_brief_enabled():
            logger.info("BIM 日报推送已禁用，跳过")
            return
        self._bim_brief_running = True
        from src.core.bim_daily_brief_service import push_bim_daily_brief

        scheduler_cfg = self.schedule_config.get("scheduler", {})
        top_n = int(scheduler_cfg.get("bim_brief_top_n", 10))
        try:
            result = await asyncio.to_thread(
                push_bim_daily_brief,
                None,
                days=1,
                top_n=top_n,
                dry_run=False,
            )
            if result.get("skipped"):
                logger.info("BIM 日报未推送: %s", result.get("reason") or "webhook 未配置")
            elif result.get("ok"):
                brief = result.get("brief") or {}
                logger.info(
                    "BIM 日报推送完成: date=%s count=%s",
                    brief.get("date"),
                    brief.get("bim_count"),
                )
            else:
                logger.error("BIM 日报推送失败: %s", result.get("error") or result.get("response"))
        except Exception as exc:
            logger.exception("BIM 日报推送异常: %s", exc)
        finally:
            self._bim_brief_running = False

    def _register_bim_brief_job(self) -> None:
        from src.core.chat_scheduled_tasks import TASK_TYPE_BIM_DAILY_BRIEF, has_enabled_chat_bim_brief_task

        if has_enabled_chat_bim_brief_task(TASK_TYPE_BIM_DAILY_BRIEF):
            existing = self.scheduler.get_job("bim_daily_brief_feishu")
            if existing:
                existing.remove()
            logger.info("BIM 日报由 chat_scheduled_tasks 接管，跳过内置 job")
            return
        if not self._is_bim_brief_enabled():
            logger.info("BIM 日报飞书推送已禁用")
            return

        cron_expr = self._bim_brief_cron_expr()
        parts = cron_expr.split()
        if len(parts) != 5:
            logger.error("bim_brief_cron 格式无效: %s", cron_expr)
            return

        minute, hour, day, month, day_of_week = parts
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=self.scheduler.timezone,
        )
        self.scheduler.add_job(
            self._run_bim_daily_brief,
            trigger=trigger,
            id="bim_daily_brief_feishu",
            replace_existing=True,
            max_instances=1,
        )
        job = self.scheduler.get_job("bim_daily_brief_feishu")
        next_run = job.next_run_time if job else None
        logger.info("已注册 BIM 日报飞书推送: cron=%s, 下次=%s", cron_expr, next_run)

    def _is_bim_weekly_brief_enabled(self) -> bool:
        from src.core.config import get_settings

        settings = get_settings()
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        if not settings.bim_weekly_brief_enabled:
            return False
        return scheduler_cfg.get("bim_weekly_brief_enabled", True)

    def _bim_weekly_brief_cron_expr(self) -> str:
        import os

        from src.core.config import get_settings

        settings = get_settings()
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        return (
            os.environ.get("BIM_WEEKLY_BRIEF_CRON")
            or settings.bim_weekly_brief_cron
            or scheduler_cfg.get("bim_weekly_brief_cron")
            or "0 18 * * 4"
        )

    async def _run_bim_weekly_brief(self) -> None:
        if self._bim_weekly_brief_running:
            logger.info("BIM 周报推送已在运行，跳过")
            return
        if not self._is_bim_weekly_brief_enabled():
            logger.info("BIM 周报推送已禁用，跳过")
            return
        self._bim_weekly_brief_running = True
        from src.core.bim_daily_brief_service import push_bim_weekly_brief

        scheduler_cfg = self.schedule_config.get("scheduler", {})
        top_n = int(scheduler_cfg.get("bim_weekly_brief_top_n", 10))
        days = int(scheduler_cfg.get("bim_weekly_brief_days", 30))
        try:
            result = await asyncio.to_thread(
                push_bim_weekly_brief,
                None,
                days=days,
                top_n=top_n,
                dry_run=False,
                channel="auto",
            )
            if result.get("skipped"):
                logger.info("BIM 周报未推送: %s", result.get("reason") or "飞书未配置")
            elif result.get("ok"):
                brief = result.get("brief") or {}
                logger.info(
                    "BIM 周报推送完成: %s ~ %s count=%s",
                    brief.get("date"),
                    brief.get("date_end"),
                    brief.get("bim_count"),
                )
            else:
                logger.error("BIM 周报推送失败: %s", result.get("error") or result.get("response"))
        except Exception as exc:
            logger.exception("BIM 周报推送异常: %s", exc)
        finally:
            self._bim_weekly_brief_running = False

    def _register_bim_weekly_brief_job(self) -> None:
        from src.core.chat_scheduled_tasks import TASK_TYPE_BIM_WEEKLY_BRIEF, has_enabled_chat_bim_brief_task

        if has_enabled_chat_bim_brief_task(TASK_TYPE_BIM_WEEKLY_BRIEF):
            existing = self.scheduler.get_job("bim_weekly_brief_feishu")
            if existing:
                existing.remove()
            logger.info("BIM 周报由 chat_scheduled_tasks 接管，跳过内置 job")
            return
        if not self._is_bim_weekly_brief_enabled():
            logger.info("BIM 周报飞书推送已禁用")
            return

        cron_expr = self._bim_weekly_brief_cron_expr()
        parts = cron_expr.split()
        if len(parts) != 5:
            logger.error("bim_weekly_brief_cron 格式无效: %s", cron_expr)
            return

        minute, hour, day, month, day_of_week = parts
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=self.scheduler.timezone,
        )
        self.scheduler.add_job(
            self._run_bim_weekly_brief,
            trigger=trigger,
            id="bim_weekly_brief_feishu",
            replace_existing=True,
            max_instances=1,
        )
        job = self.scheduler.get_job("bim_weekly_brief_feishu")
        next_run = job.next_run_time if job else None
        logger.info("已注册 BIM 周报飞书推送: cron=%s, 下次=%s", cron_expr, next_run)

    async def _run_script_cron_job(self, job_id: str) -> None:
        from src.core.script_cron import run_script_job

        result = await asyncio.to_thread(run_script_job, job_id)
        if result.get("ok"):
            logger.info("script cron 完成: %s", job_id)
        else:
            logger.error("script cron 失败: %s — %s", job_id, result.get("error") or result.get("message"))

    def _register_script_cron_jobs(self) -> None:
        from src.core.script_cron import JOBS_PATH, build_cron_trigger, load_script_cron_jobs

        if JOBS_PATH.exists():
            self._script_cron_mtime = JOBS_PATH.stat().st_mtime
        else:
            self._script_cron_mtime = 0.0

        jobs = load_script_cron_jobs()
        registered = 0
        for job in jobs:
            if not job.get("enabled", True):
                continue
            job_id = job.get("id")
            cron_expr = job.get("cron")
            if not job_id or not cron_expr:
                continue
            try:
                trigger = build_cron_trigger(cron_expr, tz=self.scheduler.timezone)
            except Exception as exc:
                logger.error("script cron 注册失败 %s: %s", job_id, exc)
                continue
            self.scheduler.add_job(
                self._run_script_cron_job,
                trigger=trigger,
                kwargs={"job_id": job_id},
                id=f"script_cron_{job_id}",
                replace_existing=True,
                max_instances=1,
            )
            registered += 1
            ap_job = self.scheduler.get_job(f"script_cron_{job_id}")
            next_run = ap_job.next_run_time if ap_job else None
            logger.info(
                "已注册 script cron: id=%s script=%s cron=%s 下次=%s",
                job_id,
                job.get("script"),
                cron_expr,
                next_run,
            )
        logger.info("script cron 任务数: %d", registered)

    def _reload_script_cron_jobs_if_changed(self) -> None:
        from src.core.script_cron import JOBS_PATH

        if not JOBS_PATH.exists():
            mtime = 0.0
        else:
            mtime = JOBS_PATH.stat().st_mtime
        if mtime == self._script_cron_mtime:
            return
        logger.info("检测到 script_cron_jobs.json 变更，重新加载")
        for job in self.scheduler.get_jobs():
            if job.id and job.id.startswith("script_cron_"):
                job.remove()
        self._register_script_cron_jobs()

    async def _run_chat_scheduled_task(self, task_id: str) -> None:
        if task_id in self._chat_scheduled_running:
            logger.info("对话定时任务 %s 正在运行，跳过", task_id)
            return
        self._chat_scheduled_running.add(task_id)
        from src.core.chat_scheduled_tasks import run_chat_scheduled_task_async

        try:
            result = await run_chat_scheduled_task_async(
                task_id,
                browser_pool=self.browser_pool,
                pipeline=self.pipeline,
            )
            if result.get("ok"):
                logger.info("对话定时任务完成: %s — %s", task_id, result.get("message"))
            else:
                logger.error("对话定时任务失败: %s — %s", task_id, result.get("error") or result.get("message"))
        except Exception as exc:
            logger.exception("对话定时任务异常 %s: %s", task_id, exc)
        finally:
            self._chat_scheduled_running.discard(task_id)

    def _register_chat_scheduled_jobs(self) -> None:
        from src.core.chat_scheduled_tasks import JOBS_PATH, build_cron_trigger_for_task, load_chat_scheduled_tasks

        if JOBS_PATH.exists():
            self._chat_scheduled_mtime = JOBS_PATH.stat().st_mtime
        else:
            self._chat_scheduled_mtime = 0.0

        jobs = load_chat_scheduled_tasks()
        registered = 0
        for task in jobs:
            if not task.get("enabled", True):
                continue
            task_id = task.get("id")
            cron_expr = task.get("cron")
            if not task_id or not cron_expr:
                continue
            try:
                trigger = build_cron_trigger_for_task(cron_expr, tz=self.scheduler.timezone)
            except Exception as exc:
                logger.error("对话定时任务注册失败 %s: %s", task_id, exc)
                continue
            self.scheduler.add_job(
                self._run_chat_scheduled_task,
                trigger=trigger,
                kwargs={"task_id": task_id},
                id=f"chat_scheduled_{task_id}",
                replace_existing=True,
                max_instances=1,
            )
            registered += 1
            ap_job = self.scheduler.get_job(f"chat_scheduled_{task_id}")
            next_run = ap_job.next_run_time if ap_job else None
            logger.info(
                "已注册对话定时任务: id=%s site=%s cron=%s 下次=%s",
                task_id,
                task.get("site_id"),
                cron_expr,
                next_run,
            )
        logger.info("对话定时任务数: %d", registered)

    def _reload_per_site_jobs_if_changed(self) -> None:
        """sites.yaml 变更后重新加载站点配置并注册 per-site 调度。"""
        if not SITES_PATH.exists():
            mtime = 0.0
        else:
            mtime = SITES_PATH.stat().st_mtime
        if mtime == self._sites_yaml_mtime:
            return
        logger.info("检测到 sites.yaml 变更，重新加载 per-site 调度")
        self._sites_yaml_mtime = mtime
        self.sites_config = load_sites_config()
        self._sites_by_id = {s["id"]: s for s in self.sites_config.get("sites", [])}
        scheduler_cfg = self.schedule_config.get("scheduler", {})
        if not scheduler_cfg.get("per_site_schedule_enabled", True):
            return
        for job in self.scheduler.get_jobs():
            if job.id and job.id.startswith("scrape_"):
                job.remove()
        self._register_per_site_staggered_jobs()

    def _reload_chat_scheduled_jobs_if_changed(self) -> None:
        from src.core.chat_scheduled_tasks import JOBS_PATH

        if not JOBS_PATH.exists():
            mtime = 0.0
        else:
            mtime = JOBS_PATH.stat().st_mtime
        if mtime == self._chat_scheduled_mtime:
            return
        logger.info("检测到 chat_scheduled_tasks.json 变更，重新加载")
        for job in self.scheduler.get_jobs():
            if job.id and job.id.startswith("chat_scheduled_"):
                job.remove()
        self._register_chat_scheduled_jobs()
        self._unregister_legacy_bim_brief_jobs_if_chat_tasks_exist()

    def _unregister_legacy_bim_brief_jobs_if_chat_tasks_exist(self) -> None:
        """卸载 schedule.yaml 内置 BIM 日报/周报 job，避免与 JSON 任务重复执行。"""
        from src.core.chat_scheduled_tasks import (
            MIGRATED_SYSTEM_TASK_IDS,
            TASK_TYPE_BIM_DAILY_BRIEF,
            TASK_TYPE_BIM_WEEKLY_BRIEF,
            has_enabled_chat_bim_brief_task,
        )

        legacy_job_ids = (
            MIGRATED_SYSTEM_TASK_IDS[TASK_TYPE_BIM_DAILY_BRIEF],
            MIGRATED_SYSTEM_TASK_IDS[TASK_TYPE_BIM_WEEKLY_BRIEF],
        )
        for task_type, job_id in (
            (TASK_TYPE_BIM_DAILY_BRIEF, legacy_job_ids[0]),
            (TASK_TYPE_BIM_WEEKLY_BRIEF, legacy_job_ids[1]),
        ):
            if not has_enabled_chat_bim_brief_task(task_type):
                continue
            job = self.scheduler.get_job(job_id)
            if job:
                job.remove()
                logger.info("已卸载内置 %s（由 chat_scheduled_tasks 接管）", job_id)

    async def start(self, run_on_start: bool = True) -> None:
        await self.browser_pool.start()
        self.register_jobs()
        self.scheduler.start()
        self._register_daily_sync_job()
        self._register_daily_bim_sync_job()
        self._register_daily_biz_clue_sync_job()
        self._register_bim_backfill_labels_job()
        self._register_script_cron_jobs()
        self._register_chat_scheduled_jobs()
        self._unregister_legacy_bim_brief_jobs_if_chat_tasks_exist()
        self.scheduler.add_job(
            self._reload_script_cron_jobs_if_changed,
            trigger=IntervalTrigger(seconds=60),
            id="script_cron_reloader",
            replace_existing=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._reload_chat_scheduled_jobs_if_changed,
            trigger=IntervalTrigger(seconds=60),
            id="chat_scheduled_reloader",
            replace_existing=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._reload_per_site_jobs_if_changed,
            trigger=IntervalTrigger(seconds=60),
            id="per_site_reloader",
            replace_existing=True,
            max_instances=1,
        )

        if run_on_start:
            scheduler_cfg = self.schedule_config.get("scheduler", {})
            if scheduler_cfg.get("run_on_start", False):
                from src.core.site_sync import load_enabled_sites

                sites = filter_schedule_eligible_sites(load_enabled_sites())
                if sites:
                    await self._run_site(sites[0]["id"], trigger="scheduled")

    async def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
        await self.browser_pool.stop()
