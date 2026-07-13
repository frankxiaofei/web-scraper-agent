"""爬取 Run 与动作级事件存储（MongoDB 优先，文件降级）。"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from src.core.config import get_settings
from src.core.timezone_utils import as_utc
from src.core.schedule_eligibility import filter_schedule_eligible_sites
from src.core.crawl_run import RunStatus
from src.core.crawl_run import CrawlRunEvent, RunStatus, RunTrigger

logger = logging.getLogger(__name__)

RUNS_COLLECTION = "crawl_runs"
EVENTS_COLLECTION = "crawl_run_events"

_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"\[(?P<level>\w+)\] run_id=(?P<run_id>\S+) site_id=(?P<site_id>\S+) "
    r"\[(?P<event_type>\w+)\] (?P<message>.*?)"
    r"(?: url=(?P<url>\S+))?$"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_line(event: CrawlRunEvent) -> str:
    ts = event.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    level = event.level.upper()
    url = (event.data or {}).get("url")
    url_part = f" url={url}" if url else ""
    return (
        f"{ts} [{level}] run_id={event.run_id} site_id={event.site_id} "
        f"[{event.type}] {event.message}{url_part}\n"
    )


class CrawlRunStore:
    """双写：MongoDB + data/crawl_logs/{site_id}/{run_id}.log。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._log_root = settings.data_dir / "crawl_logs"
        self._log_root.mkdir(parents=True, exist_ok=True)
        self._runs_coll = None
        self._events_coll = None
        self._mongo_available = False
        self._memory_runs: dict[str, dict[str, Any]] = {}
        self._memory_events: dict[str, list[dict[str, Any]]] = {}
        self._file_lock = threading.Lock()
        self._connect_mongo(settings)

    def _connect_mongo(self, settings) -> None:
        from src.db.mongo_client import get_mongo_database

        db = get_mongo_database(settings.mongodb_uri, settings.mongodb_db)
        if db is None:
            return
        self._runs_coll = db[RUNS_COLLECTION]
        self._events_coll = db[EVENTS_COLLECTION]
        self._runs_coll.create_index("run_id", unique=True)
        self._runs_coll.create_index([("site_id", 1), ("created_at", -1)])
        self._events_coll.create_index("run_id")
        self._events_coll.create_index([("site_id", 1), ("timestamp", 1)])
        self._mongo_available = True
        logger.info("爬取 Run 日志使用 MongoDB: %s / %s", RUNS_COLLECTION, EVENTS_COLLECTION)

    def _ensure_mongo(self) -> None:
        """启动时 Mongo 未就绪则后续查询前重试连接（共享 client 带退避）。"""
        if self._mongo_available:
            return
        from src.db.mongo_client import mongo_is_available

        if not mongo_is_available():
            return
        self._connect_mongo(get_settings())

    @property
    def backend(self) -> str:
        return "mongodb" if self._mongo_available else "file"

    def _log_file_path(self, site_id: str, run_id: str) -> Path:
        site_dir = self._log_root / site_id
        site_dir.mkdir(parents=True, exist_ok=True)
        return site_dir / f"{run_id}.log"

    def create_run(
        self,
        *,
        run_id: str,
        site_id: str,
        site_name: str = "",
        trigger: RunTrigger = "scheduled",
    ) -> dict[str, Any]:
        now = _utcnow()
        log_file = str(self._log_file_path(site_id, run_id))
        doc = {
            "run_id": run_id,
            "site_id": site_id,
            "site_name": site_name,
            "trigger": trigger,
            "status": "running",
            "started_at": now,
            "finished_at": None,
            "duration_seconds": 0,
            "notices_count": 0,
            "new_count": 0,
            "error": None,
            "log_file": log_file,
            "created_at": now,
            "updated_at": now,
        }
        if self._mongo_available and self._runs_coll is not None:
            self._runs_coll.update_one(
                {"run_id": run_id},
                {"$set": doc},
                upsert=True,
            )
        else:
            self._memory_runs[run_id] = doc
        return doc

    def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        notices_count: int = 0,
        new_count: int = 0,
        error: Optional[str] = None,
        duration_seconds: int = 0,
    ) -> None:
        now = _utcnow()
        fields: dict[str, Any] = {
            "status": status,
            "finished_at": now,
            "duration_seconds": duration_seconds,
            "notices_count": notices_count,
            "new_count": new_count,
            "error": (error[:2000] if error else None),
            "updated_at": now,
        }
        if self._mongo_available and self._runs_coll is not None:
            self._runs_coll.update_one({"run_id": run_id}, {"$set": fields})
        elif run_id in self._memory_runs:
            self._memory_runs[run_id].update(fields)

    def append_event(self, event: CrawlRunEvent) -> None:
        doc = event.to_dict()
        if self._mongo_available and self._events_coll is not None:
            self._events_coll.insert_one(doc)

        if event.run_id not in self._memory_events:
            self._memory_events[event.run_id] = []
        self._memory_events[event.run_id].append(doc)

        log_path = self._log_file_path(event.site_id, event.run_id)
        line = _format_line(event)
        with self._file_lock:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line)

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        self._ensure_mongo()
        if self._mongo_available and self._runs_coll is not None:
            doc = self._runs_coll.find_one({"run_id": run_id})
            if doc:
                doc.pop("_id", None)
                return self._normalize_run(doc)
        doc = self._memory_runs.get(run_id)
        if doc:
            return self._normalize_run(doc)
        found = self._find_log_path(run_id)
        if found:
            site_id, path = found
            return self._reconstruct_run_from_log(site_id, run_id, path)
        return None

    def list_runs(
        self,
        *,
        site_id: Optional[str] = None,
        limit: int = 50,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if site_id:
            query["site_id"] = site_id
        if status:
            query["status"] = status

        if self._mongo_available and self._runs_coll is not None:
            cursor = self._runs_coll.find(query).sort("created_at", -1).limit(limit)
            return [self._normalize_run(d) for d in cursor]

        runs = list(self._memory_runs.values())
        if site_id:
            runs = [r for r in runs if r.get("site_id") == site_id]
        if status:
            runs = [r for r in runs if r.get("status") == status]
        runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return [self._normalize_run(r) for r in runs[:limit]]

    def list_latest_runs_by_site(
        self,
        site_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """批量获取各站点最近一条 run（避免 list_site_jobs 的 N+1 查询）。"""
        if not site_ids:
            return {}

        self._ensure_mongo()
        if self._mongo_available and self._runs_coll is not None:
            pipeline = [
                {"$match": {"site_id": {"$in": site_ids}}},
                {"$sort": {"created_at": -1}},
                {
                    "$group": {
                        "_id": "$site_id",
                        "doc": {"$first": "$$ROOT"},
                    }
                },
            ]
            return {
                str(row["_id"]): self._normalize_run(row["doc"])
                for row in self._runs_coll.aggregate(pipeline)
                if row.get("_id")
            }

        by_site: dict[str, dict[str, Any]] = {}
        for run in sorted(
            self._memory_runs.values(),
            key=lambda r: r.get("created_at", ""),
            reverse=True,
        ):
            sid = run.get("site_id") or ""
            if sid in site_ids and sid not in by_site:
                by_site[sid] = self._normalize_run(run)
        return by_site

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        self._ensure_mongo()
        if self._mongo_available and self._events_coll is not None:
            cursor = self._events_coll.find({"run_id": run_id}).sort("timestamp", 1)
            events = [self._normalize_event(d) for d in cursor]
            if events:
                return events

        events = self._memory_events.get(run_id, [])
        if events:
            events.sort(key=lambda e: e.get("timestamp", ""))
            return [self._normalize_event(e) for e in events]

        found = self._find_log_path(run_id)
        if found:
            site_id, path = found
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return self._parse_events_from_log(site_id, run_id, lines)
        return []

    def read_log_file(self, site_id: str, run_id: str) -> list[str]:
        path = self._log_file_path(site_id, run_id)
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    def _find_log_path(self, run_id: str) -> Optional[tuple[str, Path]]:
        for site_dir in self._log_root.iterdir():
            if not site_dir.is_dir():
                continue
            path = site_dir / f"{run_id}.log"
            if path.is_file():
                return site_dir.name, path
        return None

    @staticmethod
    def _parse_log_timestamp(ts_str: str) -> datetime:
        local_tz = datetime.now().astimezone().tzinfo
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)

    def _parse_events_from_log(
        self, site_id: str, run_id: str, lines: list[str]
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in lines:
            match = _LOG_LINE_RE.match(line.strip())
            if not match:
                continue
            data = match.groupdict()
            if data["run_id"] != run_id:
                continue
            event: dict[str, Any] = {
                "run_id": run_id,
                "site_id": site_id,
                "type": data["event_type"],
                "message": data["message"],
                "level": data["level"].lower(),
                "timestamp": self._parse_log_timestamp(data["ts"]).isoformat(),
            }
            if data.get("url"):
                event["url"] = data["url"]
                event["data"] = {"url": data["url"]}
            events.append(event)
        return events

    def _reconstruct_run_from_log(
        self, site_id: str, run_id: str, path: Path
    ) -> dict[str, Any]:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        events = self._parse_events_from_log(site_id, run_id, lines)
        status: RunStatus = "running"
        site_name = ""
        error: Optional[str] = None
        started_at: Optional[datetime] = None
        finished_at: Optional[datetime] = None

        for event in events:
            ts = datetime.fromisoformat(event["timestamp"])
            if started_at is None:
                started_at = ts
            event_type = event.get("type", "")
            level = event.get("level", "")
            if event_type == "start" and event.get("message", "").startswith("开始爬取 "):
                site_name = event["message"][len("开始爬取 ") :].strip()
            if event_type in ("done",) or level == "success":
                status = "success"
                finished_at = ts
            if event_type == "error" or level == "error":
                status = "failed"
                error = event.get("message")
                finished_at = ts
            if event_type == "skipped":
                status = "skipped"
                finished_at = ts

        now = _utcnow()
        started = started_at or now
        finished = finished_at
        duration = int((finished - started).total_seconds()) if finished else 0
        doc = {
            "run_id": run_id,
            "site_id": site_id,
            "site_name": site_name,
            "trigger": "cli",
            "status": status,
            "started_at": started,
            "finished_at": finished,
            "duration_seconds": duration,
            "notices_count": 0,
            "new_count": 0,
            "error": error,
            "log_file": str(path),
            "created_at": started,
            "updated_at": finished or now,
            "_from_log_file": True,
        }
        return self._normalize_run(doc)

    @staticmethod
    def _normalize_run(doc: dict[str, Any]) -> dict[str, Any]:
        doc.pop("_id", None)
        for key in ("started_at", "finished_at", "created_at", "updated_at"):
            val = doc.get(key)
            if isinstance(val, datetime):
                doc[key] = as_utc(val).isoformat()
        return doc

    @staticmethod
    def _normalize_event(doc: dict[str, Any]) -> dict[str, Any]:
        doc.pop("_id", None)
        val = doc.get("timestamp")
        if isinstance(val, datetime):
            doc["timestamp"] = as_utc(val).isoformat()
        data = doc.get("data") or {}
        if isinstance(data, dict) and data.get("url"):
            doc["url"] = data["url"]
        return doc

    def list_site_jobs(
        self,
        sites: list[dict[str, Any]],
        schedule_meta: dict[str, Any],
        *,
        job_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """汇总各站点定时任务视图（结合最近 run 与调度元信息）。"""
        stagger = int(schedule_meta.get("round_robin_interval_minutes", 10))
        cycle = int(schedule_meta.get("per_site_cycle_minutes") or 0)
        enabled_sites = filter_schedule_eligible_sites(
            sites,
            job_overrides=job_overrides,
        )
        if not cycle:
            cycle = max(len(enabled_sites), 1) * stagger

        results: list[dict[str, Any]] = []
        site_ids = [site.get("id", "") for site in enabled_sites if site.get("id")]
        latest_by_site = self.list_latest_runs_by_site(site_ids)
        for i, site in enumerate(enabled_sites):
            site_id = site.get("id", "")
            last_run = latest_by_site.get(site_id)
            results.append({
                "site_id": site_id,
                "site_name": site.get("name") or site_id,
                "adapter": site.get("adapter", ""),
                "schedule_index": i,
                "stagger_offset_minutes": i * stagger,
                "cycle_minutes": cycle,
                "last_run": last_run,
                "last_status": last_run.get("status") if last_run else "pending",
            })
        return results


_store: Optional[CrawlRunStore] = None
_store_lock = threading.Lock()


def get_crawl_run_store() -> CrawlRunStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = CrawlRunStore()
    return _store
