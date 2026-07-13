"""抓取失败告警骨架：写日志 + 可选 Webhook。"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class AlertManager:
    """失败率 / 连续失败告警，Webhook 由 ALERT_WEBHOOK_URL 控制。"""

    def __init__(self, webhook_url: Optional[str] = None) -> None:
        self.webhook_url = webhook_url or os.environ.get("ALERT_WEBHOOK_URL")

    def send(self, title: str, message: str, level: str = "warning") -> None:
        logger.warning("[ALERT][%s] %s: %s", level.upper(), title, message)
        if not self.webhook_url:
            logger.debug("ALERT_WEBHOOK_URL 未设置，跳过 webhook")
            return
        payload = {"title": title, "message": message, "level": level}
        try:
            httpx.post(self.webhook_url, json=payload, timeout=10.0)
            logger.info("告警已发送至 webhook")
        except Exception as e:
            logger.error("告警 webhook 发送失败: %s", e)

    def check_failure_rate(
        self,
        jobs: list[dict[str, Any]],
        *,
        threshold: float = 0.5,
        min_samples: int = 5,
    ) -> bool:
        """最近任务失败率超阈值时告警，返回是否触发。"""
        if len(jobs) < min_samples:
            return False
        failures = sum(1 for j in jobs if j.get("status") == "failure")
        rate = failures / len(jobs)
        if rate < threshold:
            return False
        self.send(
            "抓取失败率超阈值",
            f"最近 {len(jobs)} 次任务失败率 {rate:.0%}（阈值 {threshold:.0%}）",
            level="critical",
        )
        return True

    def consecutive_failure(
        self,
        site_id: str,
        count: int,
        error: Optional[str] = None,
    ) -> None:
        self.send(
            f"站点连续失败 {count} 次",
            f"site_id={site_id}, error={error or '未知'}",
            level="critical",
        )


_default_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = AlertManager()
    return _default_manager
