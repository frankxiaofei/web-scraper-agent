"""多渠道通知单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.notification.base import NotificationMessage
from src.core.notification.dingtalk import DingTalkNotifier
from src.core.notification.dispatcher import send_notifications_multi
from src.core.notification.email_notifier import EmailNotifier
from src.core.notification.sms_notifier import SmsNotifier
from src.core.notification.task_push import (
    normalize_notification_channels,
    push_task_notifications,
)
from src.core.notification.wechat_work import WeChatWorkNotifier


def test_normalize_notification_channels():
    assert normalize_notification_channels(["feishu", "dingtalk"]) == ["feishu", "dingtalk"]
    assert normalize_notification_channels("feishu,email") == ["feishu", "email"]
    with pytest.raises(ValueError, match="未知通知渠道"):
        normalize_notification_channels(["unknown"])


def test_dingtalk_notifier_not_configured():
    notifier = DingTalkNotifier()
    with patch(
        "src.core.notification.dingtalk._resolve_dingtalk_config",
        return_value={"webhook_url": "", "secret": None},
    ):
        result = notifier.send(NotificationMessage(title="t", text="body"))
    assert result.skipped is True
    assert result.reason == "not_configured"


def test_dingtalk_notifier_send_mocked():
    notifier = DingTalkNotifier()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"errcode": 0}
    mock_resp.raise_for_status = MagicMock()
    with patch(
        "src.core.notification.dingtalk._resolve_dingtalk_config",
        return_value={"webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=x", "secret": None},
    ), patch("src.core.notification.dingtalk.httpx.post", return_value=mock_resp) as mock_post:
        result = notifier.send(NotificationMessage(title="测试", text="hello"))
    assert result.ok is True
    mock_post.assert_called_once()


def test_dingtalk_notifier_sign_mocked():
    notifier = DingTalkNotifier()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"errcode": 0}
    mock_resp.raise_for_status = MagicMock()
    with patch(
        "src.core.notification.dingtalk._resolve_dingtalk_config",
        return_value={
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=x",
            "secret": "SEC123",
        },
    ), patch("src.core.notification.dingtalk.httpx.post", return_value=mock_resp) as mock_post:
        result = notifier.send(NotificationMessage(title="测试", text="hello"))
    assert result.ok is True
    called_url = mock_post.call_args.args[0]
    assert "sign=" in called_url
    assert "timestamp=" in called_url


def test_wechat_notifier_send_mocked():
    notifier = WeChatWorkNotifier()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"errcode": 0}
    mock_resp.raise_for_status = MagicMock()
    with patch(
        "src.core.notification.wechat_work._resolve_wechat_config",
        return_value={"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"},
    ), patch("src.core.notification.wechat_work.httpx.post", return_value=mock_resp):
        result = notifier.send(NotificationMessage(title="测试", text="hello"))
    assert result.ok is True


def test_email_notifier_send_mocked():
    notifier = EmailNotifier()
    mock_server = MagicMock()
    with patch(
        "src.core.notification.email_notifier._resolve_email_config",
        return_value={
            "host": "smtp.example.com",
            "port": 587,
            "user": "user",
            "password": "pass",
            "from_addr": "bot@example.com",
            "use_tls": True,
            "to": ["a@example.com"],
        },
    ), patch("src.core.notification.email_notifier.smtplib.SMTP", return_value=mock_server):
        result = notifier.send(NotificationMessage(title="测试", text="hello"))
    assert result.ok is True
    mock_server.sendmail.assert_called_once()


def test_sms_notifier_send_mocked():
    notifier = SmsNotifier()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    mock_resp.raise_for_status = MagicMock()
    with patch(
        "src.core.notification.sms_notifier._resolve_sms_config",
        return_value={
            "provider": "http",
            "api_url": "https://sms.example.com/send",
            "api_key": "key",
            "to": ["13800138000"],
        },
    ), patch("src.core.notification.sms_notifier.httpx.post", return_value=mock_resp):
        result = notifier.send(NotificationMessage(title="测试", text="hello"))
    assert result.ok is True


def test_send_notifications_multi_skips_unconfigured():
    message = NotificationMessage(title="t", text="body")
    with patch(
        "src.core.notification.dispatcher.is_channel_configured",
        side_effect=lambda ch, cfg=None: ch == "dingtalk",
    ), patch(
        "src.core.notification.dispatcher.send_notification",
        return_value=__import__(
            "src.core.notification.base", fromlist=["NotificationResult"]
        ).NotificationResult(channel="dingtalk", ok=True),
    ) as mock_send:
        result = send_notifications_multi(["feishu", "dingtalk"], message)
    assert result["ok"] is True
    assert mock_send.call_count == 2


def test_push_task_notifications_dry_run():
    task = {
        "name": "测试任务",
        "task_type": "hermes_summary",
        "notification_channels": ["dingtalk"],
        "feishu_push": True,
    }
    run_result = {"ok": True, "message": "hermes=成功"}
    with patch(
        "src.core.notification.task_push.send_notifications_multi",
        return_value={"ok": True, "skipped": True, "dry_run": True, "pushed_count": 0},
    ) as mock_multi:
        out = push_task_notifications(task, run_result, dry_run=True, skip_feishu=True)
    assert out["ok"] is True
    mock_multi.assert_called_once()
