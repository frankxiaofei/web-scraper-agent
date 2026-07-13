"""定时爬取 Run 上下文与动作级事件日志。"""

from __future__ import annotations

import contextvars
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

RunStatus = Literal["pending", "running", "success", "failed", "skipped", "cancelled"]
RunTrigger = Literal[
    "scheduled", "manual", "manual_retry", "daily_sync", "daily_bim_sync", "daily_agri_sync", "daily_biz_clue_sync", "cli"
]

EVENT_TYPES = (
    "start",
    "delay",
    "navigate",
    "fetch_list",
    "fetch_detail",
    "parse",
    "paginate",
    "rule_step",
    "intelligent",
    "save",
    "retry",
    "error",
    "done",
    "skipped",
)

_current_run: contextvars.ContextVar[Optional["CrawlRunContext"]] = contextvars.ContextVar(
    "crawl_run", default=None
)


class CrawlRunEvent(BaseModel):
    """单次爬取 run 内的动作级事件。"""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    run_id: str
    site_id: str
    type: str
    message: str
    level: Literal["info", "warn", "error", "success", "debug"] = "info"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "site_id": self.site_id,
            "type": self.type,
            "message": self.message,
            "level": self.level,
            "timestamp": self.timestamp.isoformat(),
            "data": self.data,
        }


class CrawlRunRecord(BaseModel):
    """一次爬取 run 的元数据。"""

    run_id: str
    site_id: str
    site_name: str = ""
    trigger: RunTrigger = "scheduled"
    status: RunStatus = "pending"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_seconds: int = 0
    notices_count: int = 0
    new_count: int = 0
    error: Optional[str] = None
    log_file: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "site_id": self.site_id,
            "site_name": self.site_name,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "notices_count": self.notices_count,
            "new_count": self.new_count,
            "error": self.error,
            "log_file": self.log_file,
            "created_at": self.created_at.isoformat(),
        }


class CrawlRunContext:
    """当前爬取 run 的上下文，通过 contextvar 在调用链中传递。"""

    def __init__(
        self,
        *,
        site_id: str,
        site_name: str = "",
        site_url: str = "",
        trigger: RunTrigger = "scheduled",
        run_id: Optional[str] = None,
    ) -> None:
        self.run_id = run_id or uuid.uuid4().hex
        self.site_id = site_id
        self.site_name = site_name
        self.site_url = site_url
        self.trigger = trigger
        self._store = None

    def _get_store(self):
        if self._store is None:
            from src.core.crawl_run_store import get_crawl_run_store

            self._store = get_crawl_run_store()
        return self._store

    def start(self) -> None:
        store = self._get_store()
        store.create_run(
            run_id=self.run_id,
            site_id=self.site_id,
            site_name=self.site_name,
            trigger=self.trigger,
        )
        start_data = {"url": self.site_url} if self.site_url else None
        self.emit(
            "start",
            f"开始爬取 {self.site_name or self.site_id}",
            level="info",
            data=start_data,
        )

    def finish_success(
        self,
        *,
        notices_count: int = 0,
        new_count: int = 0,
        duration_seconds: int = 0,
    ) -> None:
        self.emit(
            "done",
            f"爬取完成: {notices_count} 条公告, 新增/更新 {new_count}",
            level="success",
            data={"notices_count": notices_count, "new_count": new_count},
        )
        self._get_store().finish_run(
            self.run_id,
            status="success",
            notices_count=notices_count,
            new_count=new_count,
            duration_seconds=duration_seconds,
        )

    def finish_failed(self, error: str, *, duration_seconds: int = 0) -> None:
        self.emit("error", error, level="error")
        self.emit("done", "爬取失败", level="error")
        self._get_store().finish_run(
            self.run_id,
            status="failed",
            error=error,
            duration_seconds=duration_seconds,
        )

    def finish_skipped(self, reason: str) -> None:
        self.emit("skipped", reason, level="warn")
        self._get_store().finish_run(self.run_id, status="skipped", error=reason)

    def finish_cancelled(
        self,
        *,
        duration_seconds: int = 0,
        notices_count: int = 0,
        reason: str = "用户中止",
    ) -> None:
        self.emit("done", reason, level="warn", data={"cancelled": True})
        self._get_store().finish_run(
            self.run_id,
            status="cancelled",
            error=reason,
            duration_seconds=duration_seconds,
            notices_count=notices_count,
        )

    def emit(
        self,
        event_type: str,
        message: str,
        *,
        level: Literal["info", "warn", "error", "success", "debug"] = "info",
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        event = CrawlRunEvent(
            run_id=self.run_id,
            site_id=self.site_id,
            type=event_type,
            message=message,
            level=level,
            data=data,
        )
        try:
            self._get_store().append_event(event)
        except Exception as exc:
            logger.warning("写入 crawl run 事件失败: %s", exc)


def get_current_run() -> Optional[CrawlRunContext]:
    return _current_run.get()


def emit_crawl_event(
    event_type: str,
    message: str,
    *,
    level: Literal["info", "warn", "error", "success", "debug"] = "info",
    data: Optional[dict[str, Any]] = None,
) -> None:
    ctx = _current_run.get()
    if ctx:
        ctx.emit(event_type, message, level=level, data=data)


class crawl_run_scope:
    """async 上下文管理器：绑定当前 run 并在退出时清理。"""

    def __init__(self, ctx: CrawlRunContext) -> None:
        self.ctx = ctx
        self._token: Optional[contextvars.Token] = None

    async def __aenter__(self) -> CrawlRunContext:
        self._token = _current_run.set(self.ctx)
        self.ctx.start()
        return self.ctx

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._token is not None:
            _current_run.reset(self._token)


def new_run_id() -> str:
    return uuid.uuid4().hex
