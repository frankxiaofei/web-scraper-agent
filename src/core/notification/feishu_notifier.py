"""飞书通知适配器（复用现有 feishu_push / feishu_webhook）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.notification.base import CHANNEL_FEISHU, NotificationMessage, NotificationResult

logger = logging.getLogger(__name__)


def _resolve_feishu_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    from src.core.chat_scheduled_tasks import resolve_env_feishu_webhooks

    cfg = dict(config or {})
    webhooks = cfg.get("webhooks")
    if webhooks is None and cfg.get("webhook_url"):
        entry: dict[str, Any] = {"url": cfg["webhook_url"]}
        if cfg.get("secret"):
            entry["secret"] = cfg["secret"]
        webhooks = [entry]
    if webhooks is None:
        webhooks = [
            {k: v for k, v in item.items() if k != "source"}
            for item in resolve_env_feishu_webhooks()
        ]
    user_ids = cfg.get("user_ids") or cfg.get("receive_users") or []
    return {"webhooks": webhooks or [], "user_ids": user_ids}


class FeishuNotifier:
    channel = CHANNEL_FEISHU

    def configured(self, config: Optional[dict[str, Any]] = None) -> bool:
        cfg = _resolve_feishu_config(config)
        if cfg.get("webhooks") or cfg.get("user_ids"):
            return True
        from src.core.feishu_push import is_feishu_push_configured

        return is_feishu_push_configured("auto")

    def send(
        self,
        message: NotificationMessage,
        *,
        config: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> NotificationResult:
        from src.core.feishu_webhook import build_feishu_card_header, format_feishu_card_title
        from src.core.feishu_push import push_feishu_interactive_card_multi, send_feishu_text

        cfg = _resolve_feishu_config(config)
        webhooks = cfg.get("webhooks") or []
        user_ids = cfg.get("user_ids") or []

        if not webhooks and not user_ids:
            text_result = send_feishu_text(
                message.text or message.title,
                channel="auto",
                dry_run=dry_run,
            )
            if text_result.get("skipped"):
                return NotificationResult(
                    channel=self.channel,
                    ok=True,
                    skipped=True,
                    reason=text_result.get("reason") or "not_configured",
                )
            return NotificationResult(
                channel=self.channel,
                ok=bool(text_result.get("ok")),
                error=text_result.get("error"),
                response=text_result,
            )

        card = {
            "header": build_feishu_card_header(
                format_feishu_card_title(message.title),
                template="blue",
            ),
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": message.markdown or message.text or message.title,
                    },
                }
            ],
        }
        result = push_feishu_interactive_card_multi(
            card,
            webhooks=webhooks,
            user_ids=user_ids,
            dry_run=dry_run,
        )
        if result.get("skipped"):
            return NotificationResult(
                channel=self.channel,
                ok=True,
                skipped=True,
                reason=result.get("reason") or "not_configured",
            )
        return NotificationResult(
            channel=self.channel,
            ok=bool(result.get("ok")),
            error=result.get("error"),
            response=result,
        )
