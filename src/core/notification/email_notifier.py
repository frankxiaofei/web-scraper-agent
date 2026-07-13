"""SMTP 邮件通知。"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

from src.core.notification.base import CHANNEL_EMAIL, NotificationMessage, NotificationResult

logger = logging.getLogger(__name__)


def _parse_recipients(raw: Any) -> list[str]:
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
        if part and part not in seen and "@" in part:
            seen.add(part)
            result.append(part)
    return result


def _resolve_email_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    from src.core.config import get_settings

    settings = get_settings()
    cfg = dict(config or {})
    to_raw = cfg.get("to") or cfg.get("recipients") or settings.notification_email_to
    return {
        "host": (cfg.get("host") or settings.smtp_host or "").strip(),
        "port": int(cfg.get("port") or settings.smtp_port or 587),
        "user": (cfg.get("user") or settings.smtp_user or "").strip(),
        "password": cfg.get("password") or settings.smtp_password,
        "from_addr": (cfg.get("from") or cfg.get("from_addr") or settings.smtp_from or "").strip(),
        "use_tls": cfg.get("use_tls", settings.smtp_use_tls),
        "to": _parse_recipients(to_raw),
    }


class EmailNotifier:
    channel = CHANNEL_EMAIL

    def configured(self, config: Optional[dict[str, Any]] = None) -> bool:
        cfg = _resolve_email_config(config)
        return bool(cfg.get("host") and cfg.get("from_addr") and cfg.get("to"))

    def send(
        self,
        message: NotificationMessage,
        *,
        config: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> NotificationResult:
        cfg = _resolve_email_config(config)
        if not cfg.get("host") or not cfg.get("from_addr"):
            logger.warning("SMTP 未配置（SMTP_HOST / SMTP_FROM），跳过邮件推送")
            return NotificationResult(
                channel=self.channel,
                ok=True,
                skipped=True,
                reason="not_configured",
            )
        recipients = cfg.get("to") or []
        if not recipients:
            logger.warning("邮件收件人未配置（NOTIFICATION_EMAIL_TO），跳过邮件推送")
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

        body_text = message.text or message.title
        body_html = None
        if message.markdown:
            body_html = (
                "<pre style='font-family:monospace;white-space:pre-wrap'>"
                f"{message.markdown}</pre>"
            )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = message.title
        msg["From"] = cfg["from_addr"]
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))

        host = cfg["host"]
        port = int(cfg["port"])
        user = cfg.get("user") or ""
        password = cfg.get("password") or ""
        use_tls = bool(cfg.get("use_tls"))

        try:
            if use_tls:
                server = smtplib.SMTP(host, port, timeout=20)
                server.starttls()
            else:
                server = smtplib.SMTP(host, port, timeout=20)
            if user and password:
                server.login(user, password)
            server.sendmail(cfg["from_addr"], recipients, msg.as_string())
            server.quit()
            return NotificationResult(
                channel=self.channel,
                ok=True,
                response={"recipients": recipients},
            )
        except Exception as exc:
            logger.error("SMTP 邮件发送失败: %s", exc)
            return NotificationResult(channel=self.channel, ok=False, error=str(exc))
