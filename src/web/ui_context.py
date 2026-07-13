"""Web UI 模板共享上下文。"""

from __future__ import annotations

from typing import Any

from src.core.chat_scheduled_tasks import resolve_env_feishu_receive_user_ids
from src.core.config import get_settings


def user_avatar_initial(user_id: str) -> str:
    """从飞书 user_id 生成头像缩写，如 li_xf10 → LX。"""
    raw = (user_id or "").strip()
    if not raw:
        return "?"
    parts = [p for p in raw.replace("-", "_").split("_") if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return raw[0].upper()


def resolve_header_user() -> dict[str, str]:
    """顶栏展示的当前飞书操作员信息。"""
    user_ids = resolve_env_feishu_receive_user_ids()
    settings = get_settings()
    primary = (user_ids[0] if user_ids else (settings.feishu_receive_user or "")).strip()
    if not primary:
        return {"current_user_id": "", "current_user_initial": ""}
    return {
        "current_user_id": primary,
        "current_user_initial": user_avatar_initial(primary),
    }


def build_base_template_context(*, is_agri_service: bool = False) -> dict[str, Any]:
    settings = get_settings()
    return {
        "main_ui_url": settings.public_base_url.rstrip("/") if is_agri_service else "",
        "is_agri_service": is_agri_service,
        **resolve_header_user(),
    }
