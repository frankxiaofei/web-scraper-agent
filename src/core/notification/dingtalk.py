"""钉钉群机器人 Webhook 通知。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Any, Optional

import httpx

from src.core.notification.base import CHANNEL_DINGTALK, NotificationMessage, NotificationResult

logger = logging.getLogger(__name__)


def _resolve_dingtalk_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    from src.core.config import get_settings

    settings = get_settings()
    cfg = dict(config or {})
    webhook_url = (cfg.get("webhook_url") or settings.dingtalk_webhook_url or "").strip()
    secret = cfg.get("secret")
    if secret is None:
        secret = settings.dingtalk_secret
    if secret is not None and not str(secret).strip():
        secret = None
    return {"webhook_url": webhook_url, "secret": secret}


def _build_signed_url(webhook_url: str, secret: str) -> str:
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"


class DingTalkNotifier:
    channel = CHANNEL_DINGTALK

    def configured(self, config: Optional[dict[str, Any]] = None) -> bool:
        return bool(_resolve_dingtalk_config(config).get("webhook_url"))

    def send(
        self,
        message: NotificationMessage,
        *,
        config: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> NotificationResult:
        cfg = _resolve_dingtalk_config(config)
        webhook_url = cfg.get("webhook_url") or ""
        if not webhook_url:
            logger.warning("钉钉 Webhook 未配置（DINGTALK_WEBHOOK_URL），跳过推送")
            return NotificationResult(
                channel=self.channel,
                ok=True,
                skipped=True,
                reason="not_configured",
            )
        if dry_run:
            return NotificationResult(
                channel=self.channel,
                ok=True,
                skipped=True,
                reason="dry_run",
            )

        body: dict[str, Any]
        if message.markdown:
            body = {
                "msgtype": "markdown",
                "markdown": {
                    "title": message.title,
                    "text": message.markdown,
                },
            }
        else:
            text = message.text or message.title
            body = {"msgtype": "text", "text": {"content": text}}

        url = webhook_url
        secret = cfg.get("secret")
        if secret:
            url = _build_signed_url(webhook_url, str(secret))

        try:
            resp = httpx.post(url, json=body, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") not in (0, None):
                logger.warning("钉钉 webhook 返回错误: %s", data)
                return NotificationResult(
                    channel=self.channel,
                    ok=False,
                    error=str(data.get("errmsg") or data),
                    response=data,
                )
            return NotificationResult(channel=self.channel, ok=True, response=data)
        except Exception as exc:
            logger.error("钉钉 webhook 发送失败: %s", exc)
            return NotificationResult(channel=self.channel, ok=False, error=str(exc))
