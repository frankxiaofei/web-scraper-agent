"""飞书推送通道选择：开放平台私信 vs 群 Webhook。"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional, Union

from src.core.feishu_im_client import FeishuImClient
from src.core.feishu_webhook import FeishuWebhookClient

logger = logging.getLogger(__name__)

FeishuChannel = Literal["auto", "im", "webhook"]
ResolvedChannel = Literal["im", "webhook"]


def resolve_feishu_channel(channel: FeishuChannel = "auto") -> ResolvedChannel:
    """根据配置解析实际推送通道。"""
    normalized = (channel or "auto").strip().lower()
    if normalized not in ("auto", "im", "webhook"):
        raise ValueError(f"未知 channel: {channel!r}，可选 auto|im|webhook")

    im_client = FeishuImClient()
    webhook_client = FeishuWebhookClient()

    if normalized == "im":
        if not im_client.configured:
            raise ValueError(
                "channel=im 但未配置 FEISHU_APP_ID / FEISHU_APP_SECRET 及收件人"
            )
        return "im"
    if normalized == "webhook":
        if not webhook_client.configured:
            raise ValueError("channel=webhook 但未配置 FEISHU_WEBHOOK_URL")
        return "webhook"

    if im_client.configured:
        return "im"
    if webhook_client.configured:
        return "webhook"
    return "webhook"


def is_feishu_push_configured(channel: FeishuChannel = "auto") -> bool:
    try:
        resolved = resolve_feishu_channel(channel)
    except ValueError:
        return False
    if resolved == "im":
        return FeishuImClient().configured
    return FeishuWebhookClient().configured


def send_feishu_interactive_card(
    card: dict[str, Any],
    *,
    channel: FeishuChannel = "auto",
    dry_run: bool = False,
    im_client: Optional[FeishuImClient] = None,
    webhook_client: Optional[FeishuWebhookClient] = None,
) -> dict[str, Any]:
    """按通道发送 interactive 卡片；dry_run 或未配置时跳过。"""
    payload = {"msg_type": "interactive", "card": card}

    if dry_run:
        return {"ok": True, "skipped": True, "dry_run": True, "payload": payload}

    try:
        resolved = resolve_feishu_channel(channel)
    except ValueError as exc:
        logger.info("飞书推送跳过: %s", exc)
        return {"ok": True, "skipped": True, "reason": str(exc), "payload": payload}

    if resolved == "im":
        client = im_client or FeishuImClient()
        if not client.configured:
            return {
                "ok": True,
                "skipped": True,
                "reason": "im_not_configured",
                "payload": payload,
            }
        result = client.send_interactive_card(card)
        return {**result, "payload": payload}

    client = webhook_client or FeishuWebhookClient()
    if not client.configured:
        return {
            "ok": True,
            "skipped": True,
            "reason": "webhook_not_configured",
            "payload": payload,
        }
    result = client.send_interactive_card(card)
    return {**result, "payload": payload, "channel": "webhook"}


def send_feishu_interactive_card_to_webhooks(
    card: dict[str, Any],
    webhooks: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """向多个飞书群 Webhook 发送同一张 interactive 卡片。"""
    normalized: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in webhooks or []:
        if isinstance(item, str):
            url = item.strip()
            secret = None
            name = None
        elif isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            secret = item.get("secret")
            if secret is not None and not str(secret).strip():
                secret = None
            name = (item.get("name") or "").strip() or None
        else:
            continue
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        normalized.append({"url": url, "secret": secret, "name": name})

    payload = {"msg_type": "interactive", "card": card}
    if not normalized:
        return {
            "ok": False,
            "error": "feishu_webhooks 为空",
            "payload": payload,
            "results": [],
            "pushed_count": 0,
        }
    if dry_run:
        return {
            "ok": True,
            "skipped": True,
            "dry_run": True,
            "channel": "webhook",
            "payload": payload,
            "results": [{"url": w["url"], "skipped": True, "dry_run": True} for w in normalized],
            "pushed_count": 0,
        }

    results: list[dict[str, Any]] = []
    for webhook in normalized:
        client = FeishuWebhookClient(
            webhook_url=webhook["url"],
            secret=webhook.get("secret"),
        )
        if not client.configured:
            results.append(
                {
                    "url": webhook["url"],
                    "name": webhook.get("name"),
                    "ok": False,
                    "skipped": True,
                    "reason": "webhook_not_configured",
                }
            )
            continue
        result = client.send_interactive_card(card)
        results.append(
            {
                "url": webhook["url"],
                "name": webhook.get("name"),
                **result,
            }
        )

    pushed = sum(1 for r in results if r.get("ok"))
    failed = [r for r in results if not r.get("ok") and not r.get("skipped")]
    ok = pushed > 0 and not failed
    return {
        "ok": ok,
        "channel": "webhook",
        "results": results,
        "pushed_count": pushed,
        "failed_count": len(failed),
        "payload": payload,
    }


def send_feishu_interactive_card_to_users(
    card: dict[str, Any],
    user_ids: list[str],
    *,
    dry_run: bool = False,
    im_client: Optional[FeishuImClient] = None,
) -> dict[str, Any]:
    """向多个飞书 user_id 私信发送同一张 interactive 卡片。"""
    normalized = [str(u).strip() for u in user_ids if u and str(u).strip()]
    if not normalized:
        return {"ok": False, "error": "feishu_user_ids 为空"}

    payload = {"msg_type": "interactive", "card": card}
    if dry_run:
        return {
            "ok": True,
            "skipped": True,
            "dry_run": True,
            "payload": payload,
            "results": [{"user_id": u, "skipped": True, "dry_run": True} for u in normalized],
            "pushed_count": 0,
        }

    client = im_client or FeishuImClient()
    if not client.app_id or not client.app_secret:
        return {
            "ok": True,
            "skipped": True,
            "reason": "im_not_configured",
            "payload": payload,
            "results": [],
            "pushed_count": 0,
        }

    results: list[dict[str, Any]] = []
    for uid in normalized:
        result = client.send_interactive_card_to_user(uid, card)
        results.append(result)

    pushed = sum(1 for r in results if r.get("ok"))
    failed = [r for r in results if not r.get("ok") and not r.get("skipped")]
    ok = pushed > 0 and not failed
    return {
        "ok": ok,
        "channel": "im",
        "results": results,
        "pushed_count": pushed,
        "failed_count": len(failed),
        "payload": payload,
    }


def push_feishu_interactive_card_multi(
    card: dict[str, Any],
    *,
    webhooks: Optional[list[dict[str, Any]]] = None,
    user_ids: Optional[list[str]] = None,
    dry_run: bool = False,
    im_client: Optional[FeishuImClient] = None,
) -> dict[str, Any]:
    """同时向群 Webhook 与人员列表推送；任一通道成功即视为 ok。"""
    payload = {"msg_type": "interactive", "card": card}
    webhook_items = list(webhooks or [])
    recipients = [str(u).strip() for u in (user_ids or []) if u and str(u).strip()]

    if dry_run:
        return {
            "ok": True,
            "skipped": True,
            "dry_run": True,
            "payload": payload,
            "webhook": {
                "ok": True,
                "skipped": True,
                "dry_run": True,
                "pushed_count": 0,
                "results": [],
            },
            "im": {
                "ok": True,
                "skipped": True,
                "dry_run": True,
                "pushed_count": 0,
                "results": [],
            },
        }

    if not webhook_items and not recipients:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_push_channels",
            "payload": payload,
        }

    webhook_result: dict[str, Any] = {"ok": True, "skipped": True, "reason": "no_webhooks"}
    im_result: dict[str, Any] = {"ok": True, "skipped": True, "reason": "no_user_ids"}

    if webhook_items:
        webhook_result = send_feishu_interactive_card_to_webhooks(
            card,
            webhook_items,
            dry_run=False,
        )
    if recipients:
        im_result = send_feishu_interactive_card_to_users(
            card,
            recipients,
            dry_run=False,
            im_client=im_client,
        )

    webhook_ok = bool(webhook_result.get("ok")) or bool(webhook_result.get("skipped"))
    im_ok = bool(im_result.get("ok")) or bool(im_result.get("skipped"))
    webhook_pushed = int(webhook_result.get("pushed_count") or 0)
    im_pushed = int(im_result.get("pushed_count") or 0)
    any_pushed = webhook_pushed > 0 or im_pushed > 0
    both_skipped = bool(webhook_result.get("skipped")) and bool(im_result.get("skipped"))
    ok = any_pushed or both_skipped

    return {
        "ok": ok,
        "payload": payload,
        "webhook": webhook_result,
        "im": im_result,
        "pushed_count": webhook_pushed + im_pushed,
        "channels": {
            "webhook": webhook_pushed,
            "im": im_pushed,
        },
    }


def send_feishu_text(
    text: str,
    *,
    channel: FeishuChannel = "auto",
    dry_run: bool = False,
    bot_name: Optional[str] = None,
    im_client: Optional[FeishuImClient] = None,
    webhook_client: Optional[FeishuWebhookClient] = None,
) -> dict[str, Any]:
    """按通道发送纯文本消息。"""
    if dry_run:
        return {"ok": True, "skipped": True, "dry_run": True, "text": text}

    try:
        resolved = resolve_feishu_channel(channel)
    except ValueError as exc:
        return {"ok": True, "skipped": True, "reason": str(exc)}

    if resolved == "im":
        client = im_client or FeishuImClient()
        if not client.configured:
            return {"ok": True, "skipped": True, "reason": "im_not_configured"}
        return client.send_text(text, bot_name=bot_name)

    client = webhook_client or FeishuWebhookClient()
    if not client.configured:
        return {"ok": True, "skipped": True, "reason": "webhook_not_configured"}
    result = client.send_text(text, bot_name=bot_name)
    return {**result, "channel": "webhook"}
