"""企业微信群机器人 Webhook 通知。"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from src.core.notification.base import CHANNEL_WECHAT, NotificationMessage, NotificationResult

logger = logging.getLogger(__name__)


def _resolve_wechat_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    from src.core.config import get_settings

    settings = get_settings()
    cfg = dict(config or {})
    webhook_url = (cfg.get("webhook_url") or settings.wechat_webhook_url or "").strip()
    return {"webhook_url": webhook_url}


class WeChatWorkNotifier:
    channel = CHANNEL_WECHAT

    def configured(self, config: Optional[dict[str, Any]] = None) -> bool:
        return bool(_resolve_wechat_config(config).get("webhook_url"))

    def send(
        self,
        message: NotificationMessage,
        *,
        config: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> NotificationResult:
        cfg = _resolve_wechat_config(config)
        webhook_url = cfg.get("webhook_url") or ""
        if not webhook_url:
            logger.warning("企业微信 Webhook 未配置（WECHAT_WEBHOOK_URL），跳过推送")
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

        if message.markdown:
            body = {
                "msgtype": "markdown",
                "markdown": {"content": message.markdown},
            }
        else:
            text = message.text or message.title
            body = {"msgtype": "text", "text": {"content": text}}

        try:
            resp = httpx.post(webhook_url, json=body, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") not in (0, None):
                logger.warning("企业微信 webhook 返回错误: %s", data)
                return NotificationResult(
                    channel=self.channel,
                    ok=False,
                    error=str(data.get("errmsg") or data),
                    response=data,
                )
            return NotificationResult(channel=self.channel, ok=True, response=data)
        except Exception as exc:
            logger.error("企业微信 webhook 发送失败: %s", exc)
            return NotificationResult(channel=self.channel, ok=False, error=str(exc))
