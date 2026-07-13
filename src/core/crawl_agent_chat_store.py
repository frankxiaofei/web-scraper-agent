"""Hermes Crawl Agent 对话会话持久化（data/crawl_agent_chats/{profile}/*.json）。"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.config import get_settings

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_PERSIST_BLOCK_TYPES = frozenset({"thinking", "plan", "tool", "result", "error", "url_crawl", "ui"})
_VALID_PROFILES = frozenset({"default", "agri", "stock"})


def normalize_agent_profile(agent_profile: str = "default") -> str:
    profile = (agent_profile or "default").strip().lower()
    return profile if profile in _VALID_PROFILES else "default"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_store_dir() -> Path:
    return get_settings().data_dir / "crawl_agent_chats"


def _store_dir(agent_profile: str = "default") -> Path:
    return _legacy_store_dir() / normalize_agent_profile(agent_profile)


def _session_path(session_id: str, *, agent_profile: str = "default") -> Path:
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError("无效的 session_id")
    return _store_dir(agent_profile) / f"{session_id}.json"


def _resolve_session_path(session_id: str, *, agent_profile: str = "default") -> Optional[Path]:
    """定位会话文件：优先 profile 子目录；default 兼容旧版根目录 flat 文件。"""
    try:
        path = _session_path(session_id, agent_profile=agent_profile)
    except ValueError:
        return None
    if path.is_file():
        return path
    if normalize_agent_profile(agent_profile) == "default":
        legacy = _legacy_store_dir() / f"{session_id}.json"
        if legacy.is_file():
            return legacy
    return None


def _load_session_file(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("session_id"):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取对话会话失败 %s: %s", path, exc)
    return None


def _save_session(session: dict[str, Any]) -> None:
    profile = normalize_agent_profile(session.get("agent_profile") or "default")
    session["agent_profile"] = profile
    path = _session_path(session["session_id"], agent_profile=profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def _title_from_message(text: str, *, max_len: int = 48) -> str:
    line = (text or "").strip().replace("\n", " ")
    if not line:
        return "新对话"
    if len(line) <= max_len:
        return line
    return line[: max_len - 1] + "…"


def summarize_session(session: dict[str, Any]) -> dict[str, Any]:
    messages = session.get("messages") or []
    return {
        "session_id": session["session_id"],
        "title": session.get("title") or "新对话",
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "message_count": len(messages),
        "agent_profile": normalize_agent_profile(session.get("agent_profile") or "default"),
    }


def create_session(*, title: str = "", agent_profile: str = "default") -> dict[str, Any]:
    profile = normalize_agent_profile(agent_profile)
    now = _utcnow_iso()
    session_id = str(uuid.uuid4())
    session: dict[str, Any] = {
        "session_id": session_id,
        "agent_profile": profile,
        "title": (title or "").strip() or "新对话",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    _save_session(session)
    return session


def get_session(session_id: str, *, agent_profile: str = "default") -> Optional[dict[str, Any]]:
    path = _resolve_session_path(session_id, agent_profile=agent_profile)
    if path is None:
        return None
    session = _load_session_file(path)
    if session is None:
        return None
    expected = normalize_agent_profile(agent_profile)
    stored = normalize_agent_profile(session.get("agent_profile") or "default")
    if stored != expected and not (
        expected == "default" and session.get("agent_profile") is None and path.parent == _legacy_store_dir()
    ):
        return None
    return session


def list_sessions(*, limit: int = 50, agent_profile: str = "default") -> list[dict[str, Any]]:
    if limit < 1:
        return []
    profile = normalize_agent_profile(agent_profile)
    sessions: list[dict[str, Any]] = []
    root = _store_dir(profile)
    if root.is_dir():
        for path in root.glob("*.json"):
            session = _load_session_file(path)
            if session:
                sessions.append(summarize_session(session))
    if profile == "default":
        legacy_root = _legacy_store_dir()
        if legacy_root.is_dir():
            for path in legacy_root.glob("*.json"):
                session = _load_session_file(path)
                if session:
                    sessions.append(summarize_session(session))
    sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return sessions[:limit]


def delete_session(session_id: str, *, agent_profile: str = "default") -> bool:
    path = _resolve_session_path(session_id, agent_profile=agent_profile)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("删除对话会话失败 %s: %s", path, exc)
        return False


def delete_all_sessions(*, agent_profile: str = "default") -> int:
    """删除指定 profile 下全部会话 JSON 文件，返回成功删除数量。"""
    profile = normalize_agent_profile(agent_profile)
    deleted = 0
    root = _store_dir(profile)
    if root.is_dir():
        for path in root.glob("*.json"):
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                logger.warning("删除对话会话失败 %s: %s", path, exc)
    if profile == "default":
        legacy_root = _legacy_store_dir()
        if legacy_root.is_dir():
            for path in legacy_root.glob("*.json"):
                try:
                    path.unlink()
                    deleted += 1
                except OSError as exc:
                    logger.warning("删除对话会话失败 %s: %s", path, exc)
    return deleted


def messages_to_history(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """提取 LLM 对话 history（user/assistant 文本轮次），并截断以防 prompt 膨胀。"""
    from src.core.llm_chat import truncate_chat_history

    history: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})
    return truncate_chat_history(history)


def _clean_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        block
        for block in blocks
        if isinstance(block, dict) and block.get("type") in _PERSIST_BLOCK_TYPES
    ]


def begin_streaming_turn(
    session_id: str,
    *,
    user_message: str,
    agent_profile: str = "default",
) -> dict[str, Any]:
    """开始流式 turn：写入 user + 空 assistant（streaming=True）。"""
    session = get_session(session_id, agent_profile=agent_profile)
    if not session:
        raise LookupError("会话不存在")

    user_text = (user_message or "").strip()
    if not user_text:
        raise ValueError("user_message 不能为空")

    messages: list[dict[str, Any]] = list(session.get("messages") or [])
    if messages and messages[-1].get("streaming"):
        raise ValueError("会话已有进行中的流式 turn")

    if not messages and (session.get("title") in (None, "", "新对话")):
        session["title"] = _title_from_message(user_text)

    messages.append({"role": "user", "content": user_text})
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "blocks": [],
            "streaming": True,
        }
    )
    session["messages"] = messages
    session["streaming"] = True
    session["updated_at"] = _utcnow_iso()
    _save_session(session)
    return session


def update_streaming_turn(
    session_id: str,
    *,
    blocks: list[dict[str, Any]],
    assistant_content: str = "",
    agent_profile: str = "default",
) -> dict[str, Any]:
    """刷新进行中的 assistant blocks（partial checkpoint）。"""
    session = get_session(session_id, agent_profile=agent_profile)
    if not session:
        raise LookupError("会话不存在")

    messages: list[dict[str, Any]] = list(session.get("messages") or [])
    if len(messages) < 2 or not messages[-1].get("streaming"):
        raise ValueError("无进行中的流式 turn")

    messages[-1] = {
        "role": "assistant",
        "content": (assistant_content or "").strip(),
        "blocks": _clean_blocks(blocks),
        "streaming": True,
    }
    session["messages"] = messages
    session["streaming"] = True
    session["updated_at"] = _utcnow_iso()
    _save_session(session)
    return session


def finalize_streaming_turn(
    session_id: str,
    *,
    blocks: list[dict[str, Any]],
    assistant_content: str = "",
    agent_profile: str = "default",
) -> dict[str, Any]:
    """结束流式 turn，写入最终 blocks 并清除 streaming 标记。"""
    session = get_session(session_id, agent_profile=agent_profile)
    if not session:
        raise LookupError("会话不存在")

    messages: list[dict[str, Any]] = list(session.get("messages") or [])
    if len(messages) >= 2 and messages[-1].get("streaming"):
        messages[-1] = {
            "role": "assistant",
            "content": (assistant_content or "").strip(),
            "blocks": _clean_blocks(blocks),
        }
        session["messages"] = messages
        session.pop("streaming", None)
        session.pop("active_hermes_run_id", None)
        session["updated_at"] = _utcnow_iso()
        _save_session(session)
        return session

    return append_turn(
        session_id,
        user_message=_last_user_message(messages) or "",
        blocks=blocks,
        assistant_content=assistant_content,
        agent_profile=agent_profile,
    )


def abort_streaming_turn(session_id: str, *, agent_profile: str = "default") -> bool:
    """丢弃未完成的流式 turn（user + assistant 对）。"""
    session = get_session(session_id, agent_profile=agent_profile)
    if not session:
        return False
    messages: list[dict[str, Any]] = list(session.get("messages") or [])
    if len(messages) >= 2 and messages[-1].get("streaming"):
        session["messages"] = messages[:-2]
        session.pop("streaming", None)
        session["updated_at"] = _utcnow_iso()
        _save_session(session)
        return True
    return False


def set_active_hermes_run_id(
    session_id: str,
    hermes_run_id: str,
    *,
    agent_profile: str = "default",
) -> dict[str, Any]:
    """持久化 Hermes run_id，供服务重启后恢复 SSE。"""
    session = get_session(session_id, agent_profile=agent_profile)
    if not session:
        raise LookupError("会话不存在")
    run_id = (hermes_run_id or "").strip()
    if not run_id:
        raise ValueError("hermes_run_id 不能为空")
    session["active_hermes_run_id"] = run_id
    session["updated_at"] = _utcnow_iso()
    _save_session(session)
    return session


def finalize_interrupted_turn(
    session_id: str,
    *,
    agent_profile: str = "default",
    reason: str = "连接中断，任务未能恢复。可直接发送新消息继续。",
) -> dict[str, Any]:
    """将孤儿 streaming turn 落盘为 interrupted（保留已缓冲 blocks）。"""
    session = get_session(session_id, agent_profile=agent_profile)
    if not session:
        raise LookupError("会话不存在")
    if not session.get("streaming"):
        return session

    messages: list[dict[str, Any]] = list(session.get("messages") or [])
    if len(messages) < 2 or not messages[-1].get("streaming"):
        session.pop("streaming", None)
        session.pop("active_hermes_run_id", None)
        session["updated_at"] = _utcnow_iso()
        _save_session(session)
        return session

    blocks = list(messages[-1].get("blocks") or [])
    content = (messages[-1].get("content") or "").strip() or reason
    interrupted_block = {
        "type": "error",
        "content": reason,
        "data": {"interrupted": True},
    }
    if not any(b.get("data", {}).get("interrupted") for b in blocks if isinstance(b, dict)):
        blocks.append(interrupted_block)

    messages[-1] = {
        "role": "assistant",
        "content": content,
        "blocks": _clean_blocks(blocks),
    }
    session["messages"] = messages
    session.pop("streaming", None)
    session.pop("active_hermes_run_id", None)
    session["updated_at"] = _utcnow_iso()
    _save_session(session)
    return session


def _last_user_message(messages: list[dict[str, Any]]) -> Optional[str]:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return (msg.get("content") or "").strip()
    return None


def append_turn(
    session_id: str,
    *,
    user_message: str,
    blocks: list[dict[str, Any]],
    assistant_content: str = "",
    agent_profile: str = "default",
) -> dict[str, Any]:
    session = get_session(session_id, agent_profile=agent_profile)
    if not session:
        raise LookupError("会话不存在")

    user_text = (user_message or "").strip()
    if not user_text:
        raise ValueError("user_message 不能为空")

    clean_blocks = _clean_blocks(blocks)

    messages: list[dict[str, Any]] = list(session.get("messages") or [])
    if not messages and (session.get("title") in (None, "", "新对话")):
        session["title"] = _title_from_message(user_text)

    messages.append({"role": "user", "content": user_text})
    messages.append(
        {
            "role": "assistant",
            "content": (assistant_content or "").strip(),
            "blocks": clean_blocks,
        }
    )
    session["messages"] = messages
    session["updated_at"] = _utcnow_iso()
    _save_session(session)
    return session
