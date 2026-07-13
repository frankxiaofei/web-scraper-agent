"""飞书群机器人 Webhook 推送。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_FEISHU_BOT_NAME = "工程大模型平台机器人"


def get_feishu_bot_name() -> str:
    """从配置读取飞书机器人展示名称。"""
    from src.core.config import get_settings

    name = (get_settings().feishu_bot_name or "").strip()
    return name or DEFAULT_FEISHU_BOT_NAME


def format_feishu_card_title(title: str, *, bot_name: Optional[str] = None) -> str:
    """卡片 header 标题：`{bot_name} · {title}`。"""
    bot = (bot_name or get_feishu_bot_name()).strip()
    text = (title or "").strip()
    if not text:
        return bot
    return f"{bot} · {text}"


def format_feishu_text_message(text: str, *, bot_name: Optional[str] = None) -> str:
    """纯文本消息前缀：`【{bot_name}】{text}`。"""
    bot = (bot_name or get_feishu_bot_name()).strip()
    body = (text or "").strip()
    return f"【{bot}】{body}" if body else f"【{bot}】"


def build_feishu_card_header(
    title: str,
    *,
    template: str = "blue",
    bot_name: Optional[str] = None,
) -> dict[str, Any]:
    """构建带机器人身份的飞书卡片 header。"""
    return {
        "template": template,
        "title": {
            "tag": "plain_text",
            "content": format_feishu_card_title(title, bot_name=bot_name),
        },
    }


class FeishuWebhookClient:
    """飞书/Lark 自定义机器人 Webhook 客户端。"""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
    ) -> None:
        if webhook_url is None or secret is None:
            from src.core.config import get_settings

            settings = get_settings()
            if webhook_url is None:
                webhook_url = settings.feishu_webhook_url
            if secret is None:
                secret = settings.feishu_webhook_secret
        self.webhook_url = (
            webhook_url
            or os.environ.get("FEISHU_WEBHOOK_URL")
            or os.environ.get("LARK_WEBHOOK_URL")
        )
        self.secret = secret if secret is not None else os.environ.get("FEISHU_WEBHOOK_SECRET")

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url and self.webhook_url.strip())

    def _build_sign(self, timestamp: str) -> Optional[str]:
        if not self.secret:
            return None
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def send_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发送 webhook 请求，返回飞书 API 响应 JSON。"""
        if not self.configured:
            logger.info("飞书 Webhook 未配置（FEISHU_WEBHOOK_URL / LARK_WEBHOOK_URL），跳过推送")
            return {"ok": False, "skipped": True, "reason": "webhook_not_configured"}

        body = dict(payload)
        if self.secret:
            timestamp = str(int(time.time()))
            sign = self._build_sign(timestamp)
            body["timestamp"] = timestamp
            if sign:
                body["sign"] = sign

        try:
            resp = httpx.post(self.webhook_url, json=body, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") not in (0, None):
                logger.warning("飞书 webhook 返回错误: %s", data)
                return {"ok": False, "response": data}
            logger.info("飞书简报推送成功")
            return {"ok": True, "response": data}
        except Exception as exc:
            logger.error("飞书 webhook 发送失败: %s", exc)
            return {"ok": False, "error": str(exc)}

    def send_interactive_card(self, card: dict[str, Any]) -> dict[str, Any]:
        return self.send_payload({"msg_type": "interactive", "card": card})

    def send_text(self, text: str, *, bot_name: Optional[str] = None) -> dict[str, Any]:
        """发送带机器人身份前缀的纯文本消息。"""
        return self.send_payload(
            {"msg_type": "text", "content": {"text": format_feishu_text_message(text, bot_name=bot_name)}}
        )
