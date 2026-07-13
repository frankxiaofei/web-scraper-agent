"""短信通知（通用 HTTP API）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from src.core.notification.base import CHANNEL_SMS, NotificationMessage, NotificationResult

logger = logging.getLogger(__name__)


def _parse_phones(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw]
    else:
        text = str(raw).replace("\n", ",").replace(";", ",")
        parts = [p.strip() for p in text.split(",") if p.strip()]
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            result.append(part)
    return result


def _resolve_sms_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    from src.core.config import get_settings

    settings = get_settings()
    cfg = dict(config or {})
    to_raw = cfg.get("to") or cfg.get("phones") or settings.notification_sms_to
    return {
        "provider": (cfg.get("provider") or settings.sms_provider or "http").strip(),
        "api_url": (cfg.get("api_url") or settings.sms_api_url or "").strip(),
        "api_key": cfg.get("api_key") or settings.sms_api_key,
        "to": _parse_phones(to_raw),
    }


class SmsNotifier:
    channel = CHANNEL_SMS

    def configured(self, config: Optional[dict[str, Any]] = None) -> bool:
        cfg = _resolve_sms_config(config)
        return bool(cfg.get("api_url") and cfg.get("to"))

    def send(
        self,
        message: NotificationMessage,
        *,
        config: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> NotificationResult:
        cfg = _resolve_sms_config(config)
        api_url = cfg.get("api_url") or ""
        phones = cfg.get("to") or []
        if not api_url:
            logger.warning("短信 API 未配置（SMS_API_URL），跳过短信推送")
            return NotificationResult(
                channel=self.channel,
                ok=True,
                skipped=True,
                reason="not_configured",
            )
        if not phones:
            logger.warning("短信收件人未配置（NOTIFICATION_SMS_TO），跳过短信推送")
            return NotificationResult(
                channel=self.channel,
                ok=True,
                skipped=True,
                reason="no_recipients",
            )
        if dry_run:
            return NotificationResult(
                channel=self.channel,
                ok=True,
                skipped=True,
                reason="dry_run",
            )

        text = (message.text or message.title)[:500]
        payload = {
            "provider": cfg.get("provider"),
            "phones": phones,
            "content": text,
            "title": message.title,
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        api_key = cfg.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            resp = httpx.post(api_url, json=payload, headers=headers, timeout=20.0)
            resp.raise_for_status()
            data: Any
            try:
                data = resp.json()
            except Exception:
                data = {"status_code": resp.status_code, "text": resp.text[:200]}
            return NotificationResult(channel=self.channel, ok=True, response=data)
        except Exception as exc:
            logger.error("短信 API 发送失败: %s", exc)
            return NotificationResult(channel=self.channel, ok=False, error=str(exc))
