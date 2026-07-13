"""定时任务多渠道通知推送。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.notification.base import (
    CHANNEL_FEISHU,
    DEFAULT_NOTIFICATION_CHANNELS,
    NotificationMessage,
)
from src.core.notification.dispatcher import send_notifications_multi

logger = logging.getLogger(__name__)

_VALID_CHANNELS = frozenset(
    {"feishu", "dingtalk", "wechat", "email", "sms"}
)


def normalize_notification_channels(raw: Any) -> list[str]:
    """解析并校验通知渠道列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]
    elif isinstance(raw, list):
        parts = [str(p).strip().lower() for p in raw if p and str(p).strip()]
    else:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        if part not in _VALID_CHANNELS:
            raise ValueError(f"未知通知渠道: {part!r}，可选: {sorted(_VALID_CHANNELS)}")
        if part in seen:
            continue
        seen.add(part)
        result.append(part)
    return result


def resolve_task_notification_channels(
    task: dict[str, Any],
    *,
    include_default: bool = True,
) -> list[str]:
    """解析任务通知渠道：任务级 → 默认飞书。"""
    if isinstance(task.get("notification_channels"), list) and task["notification_channels"]:
        return normalize_notification_channels(task["notification_channels"])
    if include_default and task.get("feishu_push", True):
        return list(DEFAULT_NOTIFICATION_CHANNELS)
    return []


def resolve_task_channel_config(task: dict[str, Any]) -> dict[str, Any]:
    """合并任务级 channel_config 与飞书 legacy 字段。"""
    cfg: dict[str, Any] = {}
    raw = task.get("channel_config")
    if isinstance(raw, dict):
        cfg.update(raw)

    from src.core.chat_scheduled_tasks import (
        resolve_task_feishu_user_ids,
        resolve_task_feishu_webhooks,
    )

    feishu_cfg: dict[str, Any] = {}
    webhooks = resolve_task_feishu_webhooks(task, include_env_fallback=True)
    if webhooks:
        feishu_cfg["webhooks"] = webhooks
    user_ids = resolve_task_feishu_user_ids(task, include_env_fallback=True)
    if user_ids:
        feishu_cfg["user_ids"] = user_ids
    if feishu_cfg:
        cfg.setdefault(CHANNEL_FEISHU, {}).update(feishu_cfg)
    return cfg


def build_task_notification_message(
    task: dict[str, Any],
    result: dict[str, Any],
) -> NotificationMessage:
    """从任务执行结果构建通用文本通知。"""
    task_name = task.get("name") or task.get("id") or "通知任务"
    ok = bool(result.get("ok"))
    status = "成功" if ok else "失败"
    lines = [
        f"**任务**：{task_name}",
        f"**状态**：{status}",
    ]
    message = (result.get("message") or "").strip()
    if message:
        lines.append(f"**摘要**：{message}")
    error = (result.get("error") or "").strip()
    if error and not ok:
        lines.append(f"**错误**：{error[:300]}")

    task_type = task.get("task_type") or ""
    if task_type in ("hermes_summary", "hermes_prompt"):
        hermes = result.get("hermes") or {}
        output = (hermes.get("output") or "").strip()
        if output:
            from src.core.chat_scheduled_tasks import extract_hermes_push_content

            excerpt = extract_hermes_push_content(
                output,
                push_mode=str(task.get("push_mode") or "excerpt"),
                excerpt_max=int(task.get("push_excerpt_max") or 1500),
            )
            lines.append(f"**执行输出**\n{excerpt}")
    elif task_type == "incremental_sync":
        sync = result.get("sync") or {}
        new_count = sync.get("new_count", 0)
        site_name = task.get("site_name") or task.get("site_id") or ""
        lines.append(f"**站点**：{site_name}")
        lines.append(f"**新增/更新**：{new_count} 条")

    markdown = "\n".join(lines)
    text = "\n".join(line.replace("**", "") for line in lines)
    return NotificationMessage(
        title=f"{task_name} · {status}",
        text=text,
        markdown=markdown,
    )


def push_task_notifications(
    task: dict[str, Any],
    result: dict[str, Any],
    *,
    dry_run: bool = False,
    skip_feishu: bool = False,
) -> dict[str, Any]:
    """按任务配置向多渠道发送通知摘要。"""
    channels = resolve_task_notification_channels(task)
    if not channels:
        return {"ok": True, "skipped": True, "reason": "no_channels"}

    non_feishu = [c for c in channels if c != CHANNEL_FEISHU]
    if skip_feishu and not non_feishu:
        return {"ok": True, "skipped": True, "reason": "feishu_only_skipped"}

    message = build_task_notification_message(task, result)
    channel_config = resolve_task_channel_config(task)
    return send_notifications_multi(
        channels,
        message,
        channel_config=channel_config,
        dry_run=dry_run,
        skip_feishu=skip_feishu,
    )
