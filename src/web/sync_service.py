"""站点同步状态 Web 层服务。"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.timezone_utils import format_display_dt
from src.core.crawl_run import new_run_id
from src.core.crawl_run_store import get_crawl_run_store
from src.core.hermes_agent_runner import HermesTaskType, run_hermes_crawl_task
from src.core.site_sync import get_site_by_id, load_enabled_sites, resolve_max_items
from src.core.sync_cancel import sync_cancel_scope
from src.core.sync_status import get_sync_status_store

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"

_DETAIL_PROGRESS_RE = re.compile(r"抓取详情页 \((\d+)/(\d+)\)")
_PARSE_COUNT_RE = re.compile(r"(\d+)\s*条")


@dataclass
class RunningSyncTask:
    run_id: str
    cancel_event: asyncio.Event
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    asyncio_task: Optional[asyncio.Task] = None


_running_tasks: dict[str, RunningSyncTask] = {}


def _load_schedule_meta() -> dict[str, Any]:
    if not SCHEDULE_PATH.exists():
        return {}
    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("scheduler", {})


def _format_display_dt(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "—"
    formatted = format_display_dt(iso_str)
    return formatted if formatted != "—" else iso_str


def _cron_to_human(cron: str) -> str:
    parts = cron.split()
    if len(parts) == 5 and parts[0] == "0" and parts[2:] == ["*", "*", "*"]:
        return f"每天 {parts[1].zfill(2)}:00（Asia/Shanghai）"
    return cron


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed_seconds_since(started_at: Optional[datetime]) -> int:
    if not started_at:
        return 0
    return max(
        0,
        int((datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)).total_seconds()),
    )


def _abort_orphan_sync(site_id: str, *, error: str) -> dict[str, Any]:
    """清理孤儿同步：无内存任务但 sync_status / crawl_run 仍显示运行中。"""
    run_store = get_crawl_run_store()
    status_store = get_sync_status_store()
    cancelled_runs: list[str] = []

    for run in run_store.list_runs(site_id=site_id, limit=20, status="running"):
        run_id = run["run_id"]
        started = _parse_iso_dt(run.get("started_at"))
        run_store.finish_run(
            run_id,
            status="cancelled",
            notices_count=int(run.get("notices_count") or 0),
            error=error,
            duration_seconds=_elapsed_seconds_since(started),
        )
        cancelled_runs.append(run_id)

    if status_store.get_status(site_id) == "syncing":
        doc = status_store._get_doc(site_id) or {}
        started = _parse_iso_dt(doc.get("last_sync_at"))
        status_store.mark_cancelled(
            site_id,
            error=error,
            duration_seconds=_elapsed_seconds_since(started),
            notices_count=int(doc.get("notices_count") or 0),
        )

    return {"cancelled_runs": cancelled_runs}


def _resolve_running_run_id(site_id: str) -> tuple[Optional[str], Optional[datetime]]:
    task = _running_tasks.get(site_id)
    if task:
        return task.run_id, task.started_at

    store = get_crawl_run_store()
    runs = store.list_runs(site_id=site_id, limit=1, status="running")
    if runs:
        started = _parse_iso_dt(runs[0].get("started_at"))
        return runs[0]["run_id"], started
    return None, None


def build_run_progress(site_id: str, run_id: str, *, started_at: Optional[datetime] = None) -> dict[str, Any]:
    """从 crawl_run 记录与 events 推导进度快照。"""
    site = get_site_by_id(site_id)
    max_items = resolve_max_items(site) if site else 120

    store = get_crawl_run_store()
    run = store.get_run(run_id) or {}
    events = store.list_events(run_id)

    progress_items = 0
    progress_max = max_items
    current_step = ""
    latest_event_type = ""
    latest_event_message = ""

    for event in reversed(events):
        etype = event.get("type") or ""
        message = event.get("message") or ""
        if not latest_event_message:
            latest_event_type = etype
            latest_event_message = message
            current_step = etype

        if etype == "fetch_detail":
            match = _DETAIL_PROGRESS_RE.search(message)
            if match:
                progress_items = int(match.group(1))
                # 事件分母为实际列表条数，展示统一用配置上限
                current_step = "fetch_detail"
                break

    if progress_items == 0:
        list_count = 0
        for event in events:
            if event.get("type") == "parse":
                match = _PARSE_COUNT_RE.search(event.get("message") or "")
                if match:
                    list_count = max(list_count, int(match.group(1)))
        if list_count > 0:
            progress_items = min(list_count, progress_max)
            current_step = current_step or "parse"

    run_started = started_at or _parse_iso_dt(run.get("started_at"))
    elapsed_seconds = 0
    if run_started:
        elapsed_seconds = max(
            0,
            int((datetime.now(timezone.utc) - run_started.astimezone(timezone.utc)).total_seconds()),
        )

    notices_count = int(run.get("notices_count") or progress_items or 0)
    status = run.get("status") or "running"
    progress_percent: Optional[float] = None
    if progress_max > 0:
        progress_percent = round(min(progress_items, progress_max) / progress_max * 100, 1)

    return {
        "site_id": site_id,
        "run_id": run_id,
        "status": status,
        "is_running": status == "running",
        "notices_count": notices_count,
        "events_count": len(events),
        "latest_event_type": latest_event_type,
        "latest_event_message": latest_event_message,
        "current_step": current_step,
        "elapsed_seconds": elapsed_seconds,
        "progress_items": progress_items,
        "max_items": progress_max,
        "progress_percent": progress_percent,
    }


class SyncService:
    def __init__(self) -> None:
        self._store = get_sync_status_store()
        self._schedule = _load_schedule_meta()

    @property
    def backend(self) -> str:
        return self._store.backend

    def get_schedule_info(self) -> dict[str, Any]:
        cron = self._schedule.get("daily_sync_cron", "0 2 * * *")
        agri_cron = self._schedule.get("daily_agri_sync_cron", "0 4 * * *")
        enabled = bool(self._schedule.get("daily_sync_enabled", True))
        agri_enabled = bool(self._schedule.get("daily_agri_sync_enabled", True))
        biz_cron = self._schedule.get("daily_biz_clue_sync_cron", "30 6 * * *")
        biz_enabled = bool(self._schedule.get("daily_biz_clue_sync_enabled", True))
        next_at = self._store.get_next_scheduled_at()
        next_fmt = _format_display_dt(next_at.isoformat() if next_at else None)
        if next_fmt == "—" and enabled:
            next_fmt = "启动调度器后自动计算"
        return {
            "daily_sync_enabled": enabled,
            "daily_sync_cron": cron,
            "daily_sync_cron_human": _cron_to_human(cron),
            "daily_agri_sync_enabled": agri_enabled,
            "daily_agri_sync_cron": agri_cron,
            "daily_agri_sync_cron_human": _cron_to_human(agri_cron),
            "daily_biz_clue_sync_enabled": biz_enabled,
            "daily_biz_clue_sync_cron": biz_cron,
            "daily_biz_clue_sync_cron_human": _cron_to_human(biz_cron),
            "daily_biz_clue_brief_cron": self._schedule.get("daily_biz_clue_brief_cron", "30 8 * * *"),
            "next_scheduled_at": next_fmt,
            "stagger_seconds": int(self._schedule.get("daily_sync_stagger_seconds", 30)),
        }

    def _get_bim_totals_by_site(self) -> dict[str, int]:
        try:
            from src.core.config import get_settings
            from src.db.mongo_repository import get_cached_bim_totals_by_source

            settings = get_settings()
            return get_cached_bim_totals_by_source(
                uri=settings.mongodb_uri,
                db_name=settings.mongodb_db,
                collection_name=settings.mongodb_collection,
            )
        except Exception:
            logger.debug("读取 BIM 统计失败", exc_info=True)
        return {}

    def list_status(self, *, include_bim_totals: bool = False) -> dict[str, Any]:
        sites = load_enabled_sites()
        rows = self._store.list_all(sites)
        bim_totals = self._get_bim_totals_by_site() if include_bim_totals else {}
        run_store = get_crawl_run_store()
        running_site_ids = {
            r.get("site_id")
            for r in run_store.list_runs(status="running", limit=100)
            if r.get("site_id")
        }
        stale_site_ids: list[str] = []
        for row in rows:
            site_id = row["site_id"]
            row["last_sync_at_fmt"] = _format_display_dt(row.get("last_sync_at"))
            row["last_success_at_fmt"] = _format_display_dt(row.get("last_success_at"))
            row["bim_total"] = bim_totals.get(site_id, 0)
            stale = (
                site_id not in _running_tasks
                and (
                    row.get("status") == "syncing"
                    or site_id in running_site_ids
                )
            )
            row["stale"] = stale
            if stale:
                stale_site_ids.append(site_id)
            if self.is_syncing(site_id):
                progress = self.get_sync_progress(site_id)
                if progress:
                    row["sync_progress"] = progress
        schedule = self.get_schedule_info()
        summary = {
            "total": len(rows),
            "pending": sum(1 for r in rows if r["status"] == "pending"),
            "syncing": sum(1 for r in rows if r["status"] == "syncing"),
            "completed": sum(1 for r in rows if r["status"] == "completed"),
            "failed": sum(1 for r in rows if r["status"] == "failed"),
            "cancelled": sum(1 for r in rows if r["status"] == "cancelled"),
            "stale": len(stale_site_ids),
        }
        return {
            "backend": self.backend,
            "schedule": schedule,
            "summary": summary,
            "sites": rows,
        }

    def is_stale_sync(self, site_id: str) -> bool:
        """内存无活跃任务，但持久化状态仍显示运行中（常见于服务重启）。"""
        if site_id in _running_tasks:
            return False
        if self._store.get_status(site_id) == "syncing":
            return True
        return bool(get_crawl_run_store().list_runs(site_id=site_id, limit=1, status="running"))

    def detect_stale_syncs(self) -> list[str]:
        return [
            site.get("id", "")
            for site in load_enabled_sites()
            if site.get("id") and self.is_stale_sync(site["id"])
        ]

    def is_syncing(self, site_id: str) -> bool:
        if site_id in _running_tasks:
            return True
        return self._store.get_status(site_id) == "syncing"

    def get_sync_progress(self, site_id: str) -> Optional[dict[str, Any]]:
        run_id, started_at = _resolve_running_run_id(site_id)
        if not run_id:
            return None
        progress = build_run_progress(site_id, run_id, started_at=started_at)
        if not progress["is_running"] and site_id not in _running_tasks:
            return None
        return progress

    def get_sync_status(self, site_id: str) -> dict[str, Any]:
        site = get_site_by_id(site_id)
        if not site:
            return {"ok": False, "message": "站点不存在", "site_id": site_id}

        doc = self._store.ensure_site(site_id, site.get("name") or site_id, site.get("url") or "")
        stale = self.is_stale_sync(site_id)
        payload: dict[str, Any] = {
            "ok": True,
            "site_id": site_id,
            "sync_status": doc.get("status"),
            "is_running": self.is_syncing(site_id),
            "stale": stale,
        }
        progress = self.get_sync_progress(site_id)
        if progress:
            payload["progress"] = progress
            payload["run_id"] = progress["run_id"]
        elif doc.get("status") == "syncing":
            run_id, started_at = _resolve_running_run_id(site_id)
            if run_id:
                payload["run_id"] = run_id
                payload["progress"] = build_run_progress(site_id, run_id, started_at=started_at)
        return payload

    async def trigger_sync(self, site_id: str) -> dict[str, Any]:
        return await self._start_sync(site_id, trigger="manual")

    async def retry_sync(self, site_id: str) -> dict[str, Any]:
        site = get_site_by_id(site_id)
        if not site:
            return {"ok": False, "message": "站点不存在"}
        if not site.get("enabled"):
            return {"ok": False, "message": "站点未启用"}
        return await self._start_sync(site_id, trigger="manual_retry")

    async def cancel_sync(self, site_id: str) -> dict[str, Any]:
        task = _running_tasks.get(site_id)
        if task:
            task.cancel_event.set()
            return {
                "ok": True,
                "message": "已发送中止信号",
                "site_id": site_id,
                "run_id": task.run_id,
            }

        if self.is_stale_sync(site_id):
            result = _abort_orphan_sync(site_id, error="用户中止（孤儿任务已清理）")
            return {
                "ok": True,
                "message": "同步任务已不在运行，已清理残留状态",
                "site_id": site_id,
                "stale": True,
                "cancelled_runs": result["cancelled_runs"],
            }

        return {"ok": False, "message": "该站点无运行中的任务", "site_id": site_id}

    def reset_sync_status(self, site_id: str) -> dict[str, Any]:
        site = get_site_by_id(site_id)
        if not site:
            return {"ok": False, "message": "站点不存在", "site_id": site_id}
        if site_id in _running_tasks:
            return {"ok": False, "message": "站点正在运行中，请先中止", "site_id": site_id}
        if not self.is_stale_sync(site_id):
            return {"ok": False, "message": "当前状态无需重置", "site_id": site_id}

        result = _abort_orphan_sync(site_id, error="状态已重置")
        return {
            "ok": True,
            "message": "已重置同步状态",
            "site_id": site_id,
            "cancelled_runs": result["cancelled_runs"],
        }

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = get_crawl_run_store().get_run(run_id)
        if not run:
            return {"ok": False, "message": "Run 不存在", "run_id": run_id}
        if run.get("status") != "running":
            return {"ok": False, "message": "Run 未在运行中", "run_id": run_id}
        return await self.cancel_sync(run["site_id"])

    async def _start_sync(self, site_id: str, *, trigger: str) -> dict[str, Any]:
        if self.is_syncing(site_id):
            return {
                "ok": False,
                "status": "skipped",
                "message": "该站点正在运行中",
                "site_id": site_id,
            }

        run_id = new_run_id()
        site = get_site_by_id(site_id)
        site_name = (site or {}).get("name") or site_id
        get_crawl_run_store().create_run(
            run_id=run_id,
            site_id=site_id,
            site_name=site_name,
            trigger=trigger,  # type: ignore[arg-type]
        )
        cancel_event = asyncio.Event()
        started_at = datetime.now(timezone.utc)
        _running_tasks[site_id] = RunningSyncTask(
            run_id=run_id,
            cancel_event=cancel_event,
            started_at=started_at,
        )

        async def _run() -> None:
            try:
                async with sync_cancel_scope(cancel_event):
                    await run_hermes_crawl_task(
                        HermesTaskType.SITE_SYNC,
                        site_id=site_id,
                        trigger=trigger,
                        run_id=run_id,
                        update_status=True,
                    )
            except Exception:
                logger.exception("后台同步失败: %s (trigger=%s)", site_id, trigger)
            finally:
                _running_tasks.pop(site_id, None)

        asyncio_task = asyncio.create_task(_run())
        _running_tasks[site_id].asyncio_task = asyncio_task
        return {
            "ok": True,
            "status": "started",
            "message": "已加入后台同步队列",
            "site_id": site_id,
            "run_id": run_id,
        }


_service: Optional[SyncService] = None


def get_sync_service() -> SyncService:
    global _service
    if _service is None:
        _service = SyncService()
    return _service
