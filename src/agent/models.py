"""Agent 爬取数据模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ListStrategy(str, Enum):
    DOM = "dom"
    API = "api"
    SPA = "spa"
    ADAPTER = "adapter"


class CrawlPlan(BaseModel):
    """LLM 或规则层生成的爬取计划。"""

    site_id: str
    site_name: str = ""
    base_url: str
    list_url: Optional[str] = None
    list_strategy: ListStrategy = ListStrategy.DOM
    selectors: Optional[dict[str, str]] = None
    api_endpoint: Optional[str] = None
    detail_pattern: Optional[str] = None
    pagination_hint: Optional[str] = None
    suggested_max_depth: int = 2
    use_existing_adapter: bool = False
    adapter_name: Optional[str] = None
    reasoning: Optional[str] = None


class CrawlStep(BaseModel):
    """计划中的单步操作。"""

    action: str
    url: Optional[str] = None
    selector: Optional[str] = None
    description: str = ""


class AgentEvent(BaseModel):
    """Agent 执行过程事件。"""

    type: str
    message: str
    level: Literal["info", "warn", "error", "success", "debug"] = "info"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: Optional[dict[str, Any]] = None

    def to_sse_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "level": self.level,
            "timestamp": self.timestamp.isoformat(),
            "data": self.data,
        }


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CrawlTaskRecord(BaseModel):
    """爬取任务记录。"""

    task_id: str
    site_id: str
    site_name: str = ""
    base_url: str = ""
    status: TaskStatus = TaskStatus.PENDING
    plan: Optional[CrawlPlan] = None
    events: list[AgentEvent] = Field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["status"] = self.status.value
        if self.plan:
            d["plan"] = self.plan.model_dump(mode="json")
        d["events"] = [e.to_sse_dict() for e in self.events[-50:]]
        return d
