"""Web UI 系统设置读写（.env）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from src.core.config import ROOT, get_settings

ENV_FILE = ROOT / ".env"

_ENV_LINE_RE = re.compile(
    r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)


def mask_secret(value: Optional[str], *, prefix: int = 4, suffix: int = 4) -> str:
    """API key 打码：保留前 prefix、后 suffix 字符。"""
    if not value:
        return ""
    raw = value.strip()
    if not raw:
        return ""
    if len(raw) <= prefix + suffix:
        return "*" * len(raw)
    return f"{raw[:prefix]}****{raw[-suffix:]}"


def is_masked_or_empty(value: Optional[str]) -> bool:
    """打码值或空字符串不覆盖已有密钥。"""
    if value is None:
        return True
    raw = value.strip()
    if not raw:
        return True
    return "****" in raw


def _unquote_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _quote_env_value(value: str) -> str:
    if not value:
        return ""
    if re.search(r"[\s#\"'\\]", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def read_env_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 .env 为 KEY -> 值（不含 export 前缀）。"""
    result: dict[str, str] = {}
    for line in read_env_lines(path):
        stripped = line.rstrip("\n")
        if not stripped or stripped.lstrip().startswith("#"):
            continue
        match = _ENV_LINE_RE.match(stripped)
        if not match:
            continue
        key = match.group(2)
        result[key] = _unquote_env_value(match.group(3))
    return result


def write_env_updates(path: Path, updates: dict[str, str]) -> None:
    """更新 .env 中指定变量，保留其余行与注释。"""
    lines = read_env_lines(path)
    found: set[str] = set()
    output: list[str] = []

    for line in lines:
        stripped = line.rstrip("\n")
        match = _ENV_LINE_RE.match(stripped) if stripped else None
        if match:
            key = match.group(2)
            if key in updates:
                found.add(key)
                prefix = match.group(1)
                value = updates[key]
                output.append(f"{prefix}{key}={_quote_env_value(value)}\n")
                continue
        output.append(line if line.endswith("\n") else line + "\n")

    missing = set(updates.keys()) - found
    if missing:
        if output and not output[-1].endswith("\n"):
            output[-1] = output[-1] + "\n"
        if output and not output[-1].endswith("\n\n"):
            output.append("\n")
        output.append("# ── 系统设置（Web UI 更新）────────────────\n")
        for key in sorted(missing):
            value = updates[key]
            output.append(f"{key}={_quote_env_value(value)}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(output), encoding="utf-8")


def get_public_settings(*, env_path: Path | None = None) -> dict[str, str]:
    """返回当前配置（API key 已打码）。"""
    settings = get_settings()
    file_vars = parse_env_file(env_path or ENV_FILE)

    llm_model = (file_vars.get("LLM_MODEL") or file_vars.get("OPENAI_MODEL") or settings.llm_model).strip()
    llm_api_key = (settings.openai_api_key or file_vars.get("OPENAI_API_KEY") or "").strip()
    hermes_key = (settings.hermes_agent_api_key or file_vars.get("HERMES_AGENT_API_KEY") or "").strip()

    return {
        "hermes_agent_url": (settings.hermes_agent_url or file_vars.get("HERMES_AGENT_URL") or "").strip(),
        "hermes_agent_chat_url": (
            settings.hermes_agent_chat_url or file_vars.get("HERMES_AGENT_CHAT_URL") or ""
        ).strip(),
        "hermes_agent_api_key": mask_secret(hermes_key),
        "llm_model": llm_model,
        "llm_api_key": mask_secret(llm_api_key),
    }


def save_settings(payload: dict[str, Any], *, env_path: Path | None = None) -> None:
    """保存配置到 .env，并清除 Hermes client 单例。"""
    path = env_path or ENV_FILE
    updates: dict[str, str] = {}

    if "hermes_agent_url" in payload:
        updates["HERMES_AGENT_URL"] = str(payload.get("hermes_agent_url") or "").strip()
    if "hermes_agent_chat_url" in payload:
        updates["HERMES_AGENT_CHAT_URL"] = str(payload.get("hermes_agent_chat_url") or "").strip()
    if "llm_model" in payload:
        model = str(payload.get("llm_model") or "").strip()
        updates["LLM_MODEL"] = model
        existing = parse_env_file(path)
        if "OPENAI_MODEL" in existing:
            updates["OPENAI_MODEL"] = model

    if "hermes_agent_api_key" in payload:
        value = str(payload.get("hermes_agent_api_key") or "").strip()
        if not is_masked_or_empty(value):
            updates["HERMES_AGENT_API_KEY"] = value

    if "llm_api_key" in payload:
        value = str(payload.get("llm_api_key") or "").strip()
        if not is_masked_or_empty(value):
            updates["OPENAI_API_KEY"] = value

    if updates:
        write_env_updates(path, updates)

    from src.core.hermes_agent_chat_client import reset_hermes_agent_chat_client

    reset_hermes_agent_chat_client()
