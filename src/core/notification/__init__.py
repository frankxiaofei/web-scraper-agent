"""统一通知通道：飞书、钉钉、企业微信、邮箱、短信。"""

from src.core.notification.base import (
    CHANNEL_EMAIL,
    CHANNEL_FEISHU,
    CHANNEL_DINGTALK,
    CHANNEL_SMS,
    CHANNEL_WECHAT,
    NOTIFICATION_CHANNELS,
    NotificationMessage,
    NotificationResult,
)
from src.core.notification.dispatcher import (
    get_channel_meta,
    is_channel_configured,
    list_configured_channels,
    send_notification,
    send_notifications_multi,
)

__all__ = [
    "CHANNEL_DINGTALK",
    "CHANNEL_EMAIL",
    "CHANNEL_FEISHU",
    "CHANNEL_SMS",
    "CHANNEL_WECHAT",
    "NOTIFICATION_CHANNELS",
    "NotificationMessage",
    "NotificationResult",
    "get_channel_meta",
    "is_channel_configured",
    "list_configured_channels",
    "send_notification",
    "send_notifications_multi",
]
