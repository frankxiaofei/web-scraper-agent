"""站点同步状态：MongoDB collection `site_sync_status` 读写与状态转换。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from src.core.config import get_settings
from src.core.timezone_utils import as_utc

logger = logging.getLogger(__name__)

SyncStatus = Literal["pending", "syncing", "completed", "failed", "cancelled"]
VALID_STATUSES: tuple[SyncStatus, ...] = (
    "pending",
    "syncing",
    "completed",
    "failed",
    "cancelled",
)

META_SITE_ID = "_meta"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    return str(value)


class SyncStatusStore:
    """站点同步状态存储（MongoDB 优先，JSON 文件降级）。"""

    COLLECTION = "site_sync_status"

    def __init__(self) -> None:
        settings = get_settings()
        self._json_path = settings.data_dir / "site_sync_status.json"
        self._coll = None
        self._mongo_available = False
        self._memory: dict[str, dict[str, Any]] = {}
        self._connect_mongo(settings)

    def _connect_mongo(self, settings) -> None:
        from src.db.mongo_client import get_mongo_database

        db = get_mongo_database(settings.mongodb_uri, settings.mongodb_db)
        if db is None:
            return
        self._coll = db[self.COLLECTION]
        self._coll.create_index("site_id", unique=True)
        self._mongo_available = True
        logger.info("同步状态使用 MongoDB: %s", self.COLLECTION)

    @property
    def backend(self) -> str:
        return "mongodb" if self._mongo_available else "json"

    def _load_json(self) -> dict[str, dict[str, Any]]:
        if self._memory:
            return self._memory
        if not self._json_path.is_file():
            return {}
        try:
            data = json.loads(self._json_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._memory = data
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取同步状态 JSON 失败: %s", e)
        return {}

    def _save_json(self) -> None:
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        self._json_path.write_text(
            json.dumps(self._memory, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def _get_doc(self, site_id: str) -> Optional[dict[str, Any]]:
        if self._mongo_available and self._coll is not None:
            doc = self._coll.find_one({"site_id": site_id})
            if doc:
                doc.pop("_id", None)
                return doc
            return None
        return self._load_json().get(site_id)

    def _upsert(self, site_id: str, fields: dict[str, Any]) -> None:
        fields["site_id"] = site_id
        fields["updated_at"] = _utcnow()
        if self._mongo_available and self._coll is not None:
            self._coll.update_one(
                {"site_id": site_id},
                {"$set": fields, "$setOnInsert": {"created_at": _utcnow()}},
                upsert=True,
            )
            return
        store = self._load_json()
        existing = store.get(site_id, {})
        existing.update(fields)
        if "created_at" not in existing:
            existing["created_at"] = _utcnow().isoformat()
        store[site_id] = existing
        self._memory = store
        self._save_json()

    def ensure_site(self, site_id: str, site_name: str, site_url: str = "") -> dict[str, Any]:
        doc = self._get_doc(site_id)
        if doc:
            return self._normalize(doc, site_url=site_url)
        self._upsert(
            site_id,
            {
                "site_name": site_name,
                "site_url": site_url,
                "status": "pending",
                "last_sync_at": None,
                "last_success_at": None,
                "last_error": None,
                "notices_count": 0,
                "new_count": 0,
                "bim_count": 0,
                "duration_seconds": 0,
                "next_scheduled_at": None,
            },
        )
        doc = self._get_doc(site_id) or {}
        return self._normalize(doc, site_url=site_url)

    def mark_syncing(self, site_id: str, site_name: str, site_url: str = "") -> None:
        self._upsert(
            site_id,
            {
                "site_name": site_name,
                "site_url": site_url,
                "status": "syncing",
                "last_sync_at": _utcnow(),
                "last_error": None,
            },
        )

    def mark_completed(
        self,
        site_id: str,
        *,
        notices_count: int,
        new_count: int,
        duration_seconds: int,
        bim_count: int = 0,
        agri_count: int = 0,
    ) -> None:
        now = _utcnow()
        self._upsert(
            site_id,
            {
                "status": "completed",
                "last_sync_at": now,
                "last_success_at": now,
                "last_error": None,
                "notices_count": notices_count,
                "new_count": new_count,
                "bim_count": bim_count,
                "agri_count": agri_count,
                "duration_seconds": duration_seconds,
            },
        )

    def mark_failed(
        self,
        site_id: str,
        *,
        error: str,
        duration_seconds: int = 0,
    ) -> None:
        self._upsert(
            site_id,
            {
                "status": "failed",
                "last_sync_at": _utcnow(),
                "last_error": error[:2000] if error else "未知错误",
                "duration_seconds": duration_seconds,
            },
        )

    def mark_cancelled(
        self,
        site_id: str,
        *,
        error: str = "用户中止",
        duration_seconds: int = 0,
        notices_count: int = 0,
    ) -> None:
        self._upsert(
            site_id,
            {
                "status": "cancelled",
                "last_sync_at": _utcnow(),
                "last_error": error[:2000] if error else "用户中止",
                "duration_seconds": duration_seconds,
                "notices_count": notices_count,
            },
        )

    def set_next_scheduled_at(self, dt: datetime) -> None:
        self._upsert(
            META_SITE_ID,
            {
                "site_name": "调度元信息",
                "status": "pending",
                "next_scheduled_at": dt,
            },
        )

    def get_next_scheduled_at(self) -> Optional[datetime]:
        doc = self._get_doc(META_SITE_ID)
        if not doc:
            return None
        raw = doc.get("next_scheduled_at")
        if isinstance(raw, datetime):
            return as_utc(raw)
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                pass
        return None

    def get_daily_sync_cron(self) -> Optional[str]:
        doc = self._get_doc(META_SITE_ID)
        if doc:
            return doc.get("daily_sync_cron")
        return None

    def set_schedule_meta(self, *, cron: str, next_run: Optional[datetime] = None) -> None:
        fields: dict[str, Any] = {
            "site_name": "调度元信息",
            "status": "pending",
            "daily_sync_cron": cron,
        }
        if next_run:
            fields["next_scheduled_at"] = next_run
        self._upsert(META_SITE_ID, fields)

    @staticmethod
    def _normalize(doc: dict[str, Any], site_url: str = "") -> dict[str, Any]:
        status = doc.get("status", "pending")
        if status not in VALID_STATUSES:
            status = "pending"
        return {
            "site_id": doc.get("site_id", ""),
            "site_name": doc.get("site_name") or doc.get("site_id") or "—",
            "site_url": doc.get("site_url") or site_url,
            "status": status,
            "last_sync_at": _format_dt(doc.get("last_sync_at")),
            "last_success_at": _format_dt(doc.get("last_success_at")),
            "last_error": doc.get("last_error") or "",
            "notices_count": int(doc.get("notices_count") or 0),
            "new_count": int(doc.get("new_count") or 0),
            "bim_count": int(doc.get("bim_count") or 0),
            "duration_seconds": int(doc.get("duration_seconds") or 0),
            "next_scheduled_at": _format_dt(doc.get("next_scheduled_at")),
        }

    def get_status(self, site_id: str) -> Optional[str]:
        doc = self._get_doc(site_id)
        if not doc:
            return None
        return doc.get("status")

    def list_all(self, sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for site in sites:
            site_id = site.get("id", "")
            if not site_id:
                continue
            doc = self._get_doc(site_id)
            if doc:
                row = self._normalize(doc, site_url=site.get("url", ""))
            else:
                row = {
                    "site_id": site_id,
                    "site_name": site.get("name") or site_id,
                    "site_url": site.get("url") or "",
                    "status": "pending",
                    "last_sync_at": None,
                    "last_success_at": None,
                    "last_error": "",
                    "notices_count": 0,
                    "new_count": 0,
                    "bim_count": 0,
                    "duration_seconds": 0,
                    "next_scheduled_at": None,
                }
            row["enabled"] = bool(site.get("enabled", False))
            row["adapter"] = site.get("adapter", "")
            results.append(row)
        results.sort(key=lambda r: (not r["enabled"], r["site_name"]))
        return results


_store: Optional[SyncStatusStore] = None


def get_sync_status_store() -> SyncStatusStore:
    global _store
    if _store is None:
        _store = SyncStatusStore()
    return _store
