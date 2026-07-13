"""通知通道基础类型与常量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


CHANNEL_FEISHU = "feishu"
CHANNEL_DINGTALK = "dingtalk"
CHANNEL_WECHAT = "wechat"
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"

NOTIFICATION_CHANNELS: list[dict[str, str]] = [
    {"value": CHANNEL_FEISHU, "label": "飞书"},
    {"value": CHANNEL_DINGTALK, "label": "钉钉"},
    {"value": CHANNEL_WECHAT, "label": "企业微信"},
    {"value": CHANNEL_EMAIL, "label": "邮箱"},
    {"value": CHANNEL_SMS, "label": "短信"},
]

DEFAULT_NOTIFICATION_CHANNELS: list[str] = [CHANNEL_FEISHU]


@dataclass
class NotificationMessage:
    """统一通知消息体。"""

    title: str
    text: str
    markdown: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotificationResult:
    """单通道发送结果。"""

    channel: str
    ok: bool
    skipped: bool = False
    reason: Optional[str] = None
    error: Optional[str] = None
    response: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "ok": self.ok,
            "skipped": self.skipped,
            "reason": self.reason,
            "error": self.error,
            "response": self.response,
        }


class BaseNotifier(Protocol):
    channel: str

    def configured(self, config: Optional[dict[str, Any]] = None) -> bool:
        ...

    def send(
        self,
        message: NotificationMessage,
        *,
        config: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> NotificationResult:
        ...
