"""定时爬取任务 Web 层服务。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.crawl_run_store import get_crawl_run_store
from src.core.schedule_eligibility import filter_schedule_eligible_sites
from src.core.site_sync import load_enabled_sites
from src.web.crawled_links import get_crawled_links
from src.web.sync_service import get_sync_service

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


def _load_schedule_config() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not SCHEDULE_PATH.exists():
        return {}, {}
    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    overrides: dict[str, dict[str, Any]] = {}
    for job in data.get("jobs", []):
        site_id = job.get("site_id")
        if site_id:
            overrides[site_id] = job
    return data.get("scheduler", {}), overrides


class ScheduleService:
    def __init__(self) -> None:
        self._store = get_crawl_run_store()
        self._schedule, self._job_overrides = _load_schedule_config()

    @property
    def backend(self) -> str:
        return self._store.backend

    def get_schedule_meta(self) -> dict[str, Any]:
        stagger = int(self._schedule.get("round_robin_interval_minutes", 10))
        sites = filter_schedule_eligible_sites(
            load_enabled_sites(),
            job_overrides=self._job_overrides,
        )
        cycle = self._schedule.get("per_site_cycle_minutes")
        if not cycle:
            cycle = max(len(sites), 1) * stagger
        return {
            "per_site_schedule_enabled": bool(
                self._schedule.get("per_site_schedule_enabled", True)
            ),
            "round_robin_interval_minutes": stagger,
            "per_site_cycle_minutes": int(cycle),
            "enabled_site_count": len(sites),
            "full_cycle_human": f"每 {stagger} 分钟触发 1 站，完整轮询 {int(cycle)} 分钟",
        }

    def list_jobs(self) -> dict[str, Any]:
        sites = load_enabled_sites()
        jobs = self._store.list_site_jobs(
            sites,
            self._schedule,
            job_overrides=self._job_overrides,
        )
        summary = {
            "total": len(jobs),
            "pending": sum(1 for j in jobs if j["last_status"] == "pending"),
            "running": sum(1 for j in jobs if j["last_status"] == "running"),
            "success": sum(1 for j in jobs if j["last_status"] == "success"),
            "failed": sum(1 for j in jobs if j["last_status"] == "failed"),
            "skipped": sum(1 for j in jobs if j["last_status"] == "skipped"),
        }
        return {
            "backend": self.backend,
            "schedule": self.get_schedule_meta(),
            "summary": summary,
            "jobs": jobs,
        }

    def list_runs(
        self,
        *,
        site_id: Optional[str] = None,
        limit: int = 50,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        runs = self._store.list_runs(site_id=site_id, limit=limit, status=status)
        return {"backend": self.backend, "runs": runs, "count": len(runs)}

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        run = self._store.get_run(run_id)
        if not run:
            return None
        events = self._store.list_events(run_id)
        log_lines = self._store.read_log_file(run["site_id"], run_id)
        crawled_links = get_crawled_links(run, events)
        return {
            "run": run,
            "events": events,
            "log_lines": log_lines,
            "crawled_links": crawled_links,
            "link_count": len(crawled_links),
        }

    async def retry_run(self, run_id: str) -> dict[str, Any]:
        run = self._store.get_run(run_id)
        if not run:
            return {"ok": False, "message": "Run 不存在"}
        sync_svc = get_sync_service()
        result = await sync_svc.retry_sync(run["site_id"])
        if result.get("ok"):
            result["source_run_id"] = run_id
        return result

    async def retry_job(self, site_id: str) -> dict[str, Any]:
        sync_svc = get_sync_service()
        return await sync_svc.retry_sync(site_id)


_service: Optional[ScheduleService] = None


def get_schedule_service() -> ScheduleService:
    global _service
    if _service is None:
        _service = ScheduleService()
    return _service
