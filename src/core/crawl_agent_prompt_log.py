"""Hermes /v1/runs 请求体调试日志 — 排查「新问题被历史淹没」。"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_PROMPT_LOGGER = logging.getLogger("crawl_agent.prompt")
_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "crawl_agent_prompt.log"
_INSTRUCTIONS_FULL_DIR = _LOG_PATH.parent / "crawl_agent_prompt_instructions"
_LOGGER_CONFIGURED = False

_BEARER_RE = re.compile(r"Bearer\s+\S+", re.I)
_API_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|token|secret)\s*[:=]\s*\S+",
    re.I,
)


def is_prompt_logging_enabled() -> bool:
    raw = os.getenv("CRAWL_AGENT_LOG_PROMPT", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _ensure_prompt_logger() -> None:
    global _LOGGER_CONFIGURED
    if _LOGGER_CONFIGURED:
        return
    _PROMPT_LOGGER.setLevel(logging.INFO)
    if not _PROMPT_LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
        _PROMPT_LOGGER.addHandler(handler)
    _PROMPT_LOGGER.propagate = False
    _LOGGER_CONFIGURED = True


def _sanitize_text(text: str) -> str:
    if not text:
        return text
    out = _BEARER_RE.sub("Bearer [REDACTED]", text)
    out = _API_KEY_RE.sub(r"\1=[REDACTED]", out)
    return out


def _truncate_instructions(text: str, *, head: int = 500, tail: int = 200) -> str:
    if len(text) <= head + tail + 20:
        return text
    return f"{text[:head]}\n…[truncated {len(text) - head - tail} chars]…\n{text[-tail:]}"


def _history_summary(history: Optional[list[dict[str, str]]]) -> dict[str, Any]:
    if not history:
        return {"count": 0, "items": []}
    items = []
    for i, msg in enumerate(history):
        content = _sanitize_text((msg.get("content") or "").strip())
        preview = content if len(content) <= 120 else f"{content[:120]}…"
        items.append(
            {
                "index": i,
                "role": msg.get("role", "user"),
                "content_len": len(content),
                "content_preview": preview,
            }
        )
    return {"count": len(history), "items": items}


def _write_instructions_full(
    *,
    timestamp: str,
    session_id: Optional[str],
    instructions: str,
) -> Optional[str]:
    if not instructions:
        return None
    try:
        _INSTRUCTIONS_FULL_DIR.mkdir(parents=True, exist_ok=True)
        sid = (session_id or "no-session").replace("/", "_")[:64]
        safe_ts = timestamp.replace(":", "").replace("+", "")
        path = _INSTRUCTIONS_FULL_DIR / f"{safe_ts}_{sid}.txt"
        path.write_text(instructions, encoding="utf-8")
        return str(path.relative_to(_LOG_PATH.parent.parent))
    except OSError:
        _PROMPT_LOGGER.debug("写入 instructions 全量文件失败", exc_info=True)
        return None


def log_hermes_run_payload(
    *,
    layer: str,
    input_text: str,
    conversation_history: Optional[list[dict[str, str]]] = None,
    instructions: Optional[str] = None,
    session_id: Optional[str] = None,
    hermes_session_id: Optional[str] = None,
    agent_profile: Optional[str] = None,
    raw_user_message: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """写入 logs/crawl_agent_prompt.log 并 INFO 摘要（默认开启，CRAWL_AGENT_LOG_PROMPT=0 关闭）。"""
    if not is_prompt_logging_enabled():
        return

    _ensure_prompt_logger()
    now = datetime.now(timezone.utc).astimezone()
    timestamp = now.isoformat(timespec="seconds")
    safe_input = _sanitize_text((input_text or "").strip())
    safe_instructions = _sanitize_text((instructions or "").strip())
    safe_history = [
        {
            "role": m.get("role", "user"),
            "content": _sanitize_text((m.get("content") or "").strip()),
        }
        for m in (conversation_history or [])
    ]

    instructions_full_path: Optional[str] = None
    if len(safe_instructions) > 700:
        instructions_full_path = _write_instructions_full(
            timestamp=timestamp,
            session_id=hermes_session_id or session_id,
            instructions=safe_instructions,
        )

    record: dict[str, Any] = {
        "timestamp": timestamp,
        "layer": layer,
        "input": safe_input,
        "conversation_history": safe_history,
        "instructions_len": len(safe_instructions),
        "instructions": _truncate_instructions(safe_instructions) if safe_instructions else "",
        "session_id": session_id,
        "hermes_session_id": hermes_session_id or session_id,
    }
    if instructions_full_path:
        record["instructions_full_file"] = instructions_full_path
    if agent_profile:
        record["agent_profile"] = agent_profile
    if raw_user_message and raw_user_message.strip() != safe_input:
        record["raw_user_message"] = _sanitize_text(raw_user_message.strip())
    if extra:
        record["extra"] = extra

    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        _PROMPT_LOGGER.warning("写入 %s 失败", _LOG_PATH, exc_info=True)

    hist_summary = _history_summary(safe_history)
    _PROMPT_LOGGER.info(
        "[hermes-prompt] layer=%s session=%s input=%r history_turns=%s instructions_len=%s",
        layer,
        hermes_session_id or session_id or "-",
        safe_input[:200] + ("…" if len(safe_input) > 200 else ""),
        hist_summary["count"],
        len(safe_instructions),
    )
    if hist_summary["items"]:
        previews = [
            f"{it['index']}:{it['role']}:{it['content_preview']!r}"
            for it in hist_summary["items"]
        ]
        _PROMPT_LOGGER.info("[hermes-prompt] history_preview=%s", " | ".join(previews))


def get_prompt_log_path() -> Path:
    return _LOG_PATH
