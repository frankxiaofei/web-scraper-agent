"""通知通道注册与分发。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.notification.base import (
    CHANNEL_DINGTALK,
    CHANNEL_EMAIL,
    CHANNEL_FEISHU,
    CHANNEL_SMS,
    CHANNEL_WECHAT,
    NOTIFICATION_CHANNELS,
    NotificationMessage,
    NotificationResult,
)
from src.core.notification.dingtalk import DingTalkNotifier
from src.core.notification.email_notifier import EmailNotifier
from src.core.notification.feishu_notifier import FeishuNotifier
from src.core.notification.sms_notifier import SmsNotifier
from src.core.notification.wechat_work import WeChatWorkNotifier

logger = logging.getLogger(__name__)

_NOTIFIERS = {
    CHANNEL_FEISHU: FeishuNotifier(),
    CHANNEL_DINGTALK: DingTalkNotifier(),
    CHANNEL_WECHAT: WeChatWorkNotifier(),
    CHANNEL_EMAIL: EmailNotifier(),
    CHANNEL_SMS: SmsNotifier(),
}

_CHANNEL_ENV_HINTS: dict[str, str] = {
    CHANNEL_FEISHU: "FEISHU_WEBHOOK_URL",
    CHANNEL_DINGTALK: "DINGTALK_WEBHOOK_URL",
    CHANNEL_WECHAT: "WECHAT_WEBHOOK_URL",
    CHANNEL_EMAIL: "SMTP_HOST + NOTIFICATION_EMAIL_TO",
    CHANNEL_SMS: "SMS_API_URL + NOTIFICATION_SMS_TO",
}


def get_notifier(channel: str):
    key = (channel or "").strip().lower()
    notifier = _NOTIFIERS.get(key)
    if not notifier:
        raise ValueError(f"未知通知渠道: {channel!r}")
    return notifier


def get_channel_meta() -> list[dict[str, Any]]:
    """返回各渠道元数据（供 UI / API）。"""
    return [
        {
            **item,
            "configured": is_channel_configured(item["value"]),
            "env_hint": _CHANNEL_ENV_HINTS.get(item["value"], ""),
        }
        for item in NOTIFICATION_CHANNELS
    ]


def is_channel_configured(channel: str, config: Optional[dict[str, Any]] = None) -> bool:
    try:
        return get_notifier(channel).configured(config)
    except ValueError:
        return False


def list_configured_channels(
    channels: Optional[list[str]] = None,
    *,
    channel_config: Optional[dict[str, Any]] = None,
) -> list[str]:
    cfg_map = channel_config or {}
    candidates = channels or list(_NOTIFIERS.keys())
    result: list[str] = []
    for ch in candidates:
        ch_cfg = cfg_map.get(ch) if isinstance(cfg_map.get(ch), dict) else None
        if is_channel_configured(ch, ch_cfg):
            result.append(ch)
    return result


def send_notification(
    channel: str,
    message: NotificationMessage,
    *,
    config: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
) -> NotificationResult:
    notifier = get_notifier(channel)
    return notifier.send(message, config=config, dry_run=dry_run)


def send_notifications_multi(
    channels: list[str],
    message: NotificationMessage,
    *,
    channel_config: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    skip_feishu: bool = False,
) -> dict[str, Any]:
    """向多个渠道发送同一消息；未配置渠道优雅跳过。"""
    if not channels:
        return {"ok": True, "skipped": True, "reason": "no_channels", "results": []}

    cfg_map = channel_config or {}
    results: list[NotificationResult] = []
    for ch in channels:
        ch_key = (ch or "").strip().lower()
        if skip_feishu and ch_key == CHANNEL_FEISHU:
            continue
        ch_cfg = cfg_map.get(ch_key) if isinstance(cfg_map.get(ch_key), dict) else None
        try:
            result = send_notification(ch_key, message, config=ch_cfg, dry_run=dry_run)
        except ValueError as exc:
            logger.warning("跳过未知通知渠道 %s: %s", ch_key, exc)
            result = NotificationResult(
                channel=ch_key,
                ok=False,
                skipped=True,
                reason=str(exc),
            )
        results.append(result)

    pushed = [r for r in results if r.ok and not r.skipped]
    failed = [r for r in results if not r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    ok = (bool(pushed) or bool(skipped)) and not failed
    return {
        "ok": ok,
        "results": [r.to_dict() for r in results],
        "pushed_count": len(pushed),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
    }

