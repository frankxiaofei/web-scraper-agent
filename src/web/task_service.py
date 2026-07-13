"""站点任务状态聚合服务（同步状态 + 定时调度 + 最近 run）。"""

from __future__ import annotations

from typing import Any, Optional

from src.core.crawl_run_store import get_crawl_run_store
from src.core.site_sync import get_site_by_id
from src.web.schedule_service import get_schedule_service
from src.web.sync_service import _format_display_dt, get_sync_service

RUN_STATUS_LABELS: dict[str, str] = {
    "pending": "待执行",
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "skipped": "跳过",
    "cancelled": "已中止",
}

SYNC_STATUS_LABELS: dict[str, str] = {
    "pending": "未同步",
    "syncing": "同步中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已中止",
}


class TaskService:
    def list_sites(self) -> dict[str, Any]:
        sync_svc = get_sync_service()
        sched_svc = get_schedule_service()
        run_store = get_crawl_run_store()

        sync_data = sync_svc.list_status(include_bim_totals=False)
        sched_data = sched_svc.list_jobs()
        job_map = {j["site_id"]: j for j in sched_data["jobs"]}
        all_site_ids = [row["site_id"] for row in sync_data["sites"]]
        latest_runs = run_store.list_latest_runs_by_site(all_site_ids)

        sites: list[dict[str, Any]] = []
        for row in sync_data["sites"]:
            sites.append(
                self._merge_site_row(
                    row,
                    job_map.get(row["site_id"], {}),
                    sched_data["schedule"],
                    sync_svc=sync_svc,
                    run_store=run_store,
                    latest_run=latest_runs.get(row["site_id"]),
                )
            )

        summary = {
            "total": len(sites),
            "sync_pending": sync_data["summary"]["pending"],
            "sync_syncing": sync_data["summary"]["syncing"],
            "sync_completed": sync_data["summary"]["completed"],
            "sync_failed": sync_data["summary"]["failed"],
            "run_success": sched_data["summary"]["success"],
            "run_failed": sched_data["summary"]["failed"],
            "run_running": sum(1 for s in sites if s.get("is_running")),
        }

        from src.core.crawl_agent_actions import enrich_action, recent_by_site

        actions_map = recent_by_site(per_site=3)
        for site in sites:
            site["agent_actions"] = [
                enrich_action(r) for r in actions_map.get(site["site_id"], [])
            ]

        return {
            "backend": {
                "sync": sync_data["backend"],
                "runs": sched_data["backend"],
            },
            "daily_sync": sync_data["schedule"],
            "crawl_schedule": sched_data["schedule"],
            "summary": summary,
            "sites": sites,
        }

    @staticmethod
    def _merge_site_row(
        row: dict[str, Any],
        job: dict[str, Any],
        crawl_schedule: dict[str, Any],
        *,
        sync_svc: Any,
        run_store: Any,
        latest_run: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        site_id = row["site_id"]
        last_run = job.get("last_run")

        # 无 crawl_rules 的 generic 等不在 list_site_jobs 中，用批量预取的最近 run
        if not last_run:
            last_run = latest_run

        sync_status = row.get("status", "pending")
        is_running = sync_status == "syncing" or sync_svc.is_syncing(site_id)

        progress: Optional[dict[str, Any]] = row.get("sync_progress")
        if is_running or (last_run and last_run.get("status") == "running"):
            progress = sync_svc.get_sync_progress(site_id) or progress

        active_run_id: Optional[str] = None
        if progress:
            active_run_id = progress.get("run_id")
            if progress.get("is_running"):
                is_running = True

        if is_running and active_run_id:
            active_run = run_store.get_run(active_run_id)
            if active_run:
                last_run = active_run
        elif is_running and last_run and last_run.get("status") == "running":
            active_run_id = last_run.get("run_id")

        last_run_status = last_run.get("status") if last_run else "pending"
        if is_running:
            last_run_status = "running"

        if last_run:
            items_count = last_run.get("notices_count") or row.get("notices_count") or 0
            new_count = last_run.get("new_count") or row.get("new_count") or 0
        else:
            items_count = row.get("notices_count") or 0
            new_count = row.get("new_count") or 0

        display_status = TaskService._resolve_display_status(
            sync_status=sync_status,
            last_run_status=last_run_status,
            is_running=is_running,
        )
        sync_stale = sync_svc.is_stale_sync(site_id) if is_running else False

        merged: dict[str, Any] = {
            **row,
            "schedule_index": job.get("schedule_index"),
            "stagger_offset_minutes": job.get("stagger_offset_minutes"),
            "cycle_minutes": job.get("cycle_minutes"),
            "last_run": last_run,
            "last_run_status": last_run_status,
            "last_run_status_label": RUN_STATUS_LABELS.get(last_run_status, last_run_status),
            "last_run_id": last_run.get("run_id") if last_run else None,
            "active_run_id": active_run_id or (last_run.get("run_id") if last_run else None),
            "last_run_started_fmt": _format_display_dt(
                last_run.get("started_at") if last_run else None
            ),
            "items_count": items_count,
            "new_count_display": new_count,
            "next_schedule_hint": TaskService._next_schedule_hint(job, crawl_schedule),
            "is_running": is_running,
            "sync_stale": sync_stale,
            "display_status": display_status,
            "sync_status_label": (
                "同步中（异常）" if sync_stale else (
                    SYNC_STATUS_LABELS["syncing"]
                    if is_running
                    else SYNC_STATUS_LABELS.get(sync_status, sync_status)
                )
            ),
        }
        if progress:
            merged["sync_progress"] = progress
        return merged

    @staticmethod
    def _resolve_display_status(
        *,
        sync_status: str,
        last_run_status: str,
        is_running: bool,
    ) -> str:
        if is_running:
            return "running"
        if sync_status == "failed" or last_run_status == "failed":
            return "failed"
        if sync_status == "cancelled" or last_run_status == "cancelled":
            return "cancelled"
        if sync_status == "completed" and last_run_status in ("success", "pending"):
            return "success"
        if sync_status == "pending" and last_run_status == "pending":
            return "pending"
        return sync_status or last_run_status or "pending"

    def get_site(self, site_id: str, *, runs_limit: int = 20) -> Optional[dict[str, Any]]:
        site = get_site_by_id(site_id)
        if not site or not site.get("enabled"):
            return None

        data = self.list_sites()
        site_row = next((s for s in data["sites"] if s["site_id"] == site_id), None)
        if not site_row:
            return None

        sched_svc = get_schedule_service()
        runs = sched_svc.list_runs(site_id=site_id, limit=runs_limit)
        for run in runs["runs"]:
            run["started_at_fmt"] = _format_display_dt(run.get("started_at"))

        return {
            "backend": data["backend"],
            "daily_sync": data["daily_sync"],
            "crawl_schedule": data["crawl_schedule"],
            "site": site_row,
            "runs": runs["runs"],
        }

    @staticmethod
    def _next_schedule_hint(job: dict[str, Any], crawl_schedule: dict[str, Any]) -> str:
        if not job:
            return "—"
        idx = job.get("schedule_index")
        offset = job.get("stagger_offset_minutes")
        stagger = crawl_schedule.get("round_robin_interval_minutes", 10)
        if idx is None or offset is None:
            return "—"
        return f"队列 #{idx} · +{offset} min（每 {stagger} min 1 站）"


_service: Optional[TaskService] = None


def get_task_service() -> TaskService:
    global _service
    if _service is None:
        _service = TaskService()
    return _service
