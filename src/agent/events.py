"""Agent 事件类型常量与工厂函数。"""

from __future__ import annotations

from typing import Any, Literal, Optional

from src.agent.models import AgentEvent

# 标准事件类型
PLAN_START = "plan_start"
PAGE_LOAD = "page_load"
ANALYZE = "analyze"
EXTRACT = "extract"
RECURSE = "recurse"
SAVE = "save"
ERROR = "error"
DONE = "done"
LOG = "log"
CANCEL = "cancel"


def make_event(
    event_type: str,
    message: str,
    *,
    level: Literal["info", "warn", "error", "success", "debug"] = "info",
    data: Optional[dict[str, Any]] = None,
) -> AgentEvent:
    return AgentEvent(type=event_type, message=message, level=level, data=data)
