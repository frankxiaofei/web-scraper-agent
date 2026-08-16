#!/usr/bin/env python3
"""Bootstrap data/hermes-agent/.env from web_scraper .env for local Hermes gateway."""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HERMES_HOME = ROOT / "data" / "hermes-agent"
HERMES_ENV = HERMES_HOME / ".env"
DEFAULT_API_KEY = "local-dev-hermes-api-key-change-me"

_LLM_ENV_SYNC = (
    ("OPENAI_API_KEY", "OPENAI_API_KEY"),
    ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    ("LLM_BASE_URL", "OPENAI_BASE_URL"),
    ("OPENAI_BASE_URL", "OPENAI_BASE_URL"),
    ("LLM_MODEL", "LLM_MODEL"),
    ("OPENAI_MODEL", "LLM_MODEL"),
)

_PLACEHOLDER_KEY = re.compile(
    r"^(?:sk-xxx(?:xxx)?|xxx|change-me|your[-_]?api[-_]?key|test|placeholder)$",
    re.IGNORECASE,
)


def _parse_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines: list[str] = []
    seen: set[str] = set()
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                key = raw.split("=", 1)[0].strip()
                if key in updates:
                    lines.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            lines.append(raw)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def is_real_api_key(value: str | None) -> bool:
    """Return True when value looks like a real API key (not empty/placeholder)."""
    key = (value or "").strip()
    if len(key) < 8:
        return False
    if _PLACEHOLDER_KEY.match(key):
        return False
    if key.lower().startswith("sk-xxx"):
        return False
    return True


def resolve_llm_model_config(ws_env: dict[str, str]) -> dict[str, str]:
    """Pick Hermes model.provider/default/base_url from web_scraper .env keys."""
    openrouter = (ws_env.get("OPENROUTER_API_KEY") or "").strip()
    deepseek = (ws_env.get("DEEPSEEK_API_KEY") or "").strip()
    openai = (ws_env.get("OPENAI_API_KEY") or "").strip()
    base_url = (ws_env.get("OPENAI_BASE_URL") or ws_env.get("LLM_BASE_URL") or "").strip()
    model = (ws_env.get("OPENAI_MODEL") or ws_env.get("LLM_MODEL") or "").strip()

    if is_real_api_key(openrouter):
        return {
            "provider": "openrouter",
            "default": model or "anthropic/claude-sonnet-4",
        }
    if is_real_api_key(deepseek):
        cfg = {"provider": "deepseek", "default": model or "deepseek-v4-flash"}
        if base_url:
            cfg["base_url"] = base_url.rstrip("/")
        return cfg
    if is_real_api_key(openai):
        cfg = {"provider": "openai-api", "default": model or "gpt-4o-mini"}
        if base_url:
            cfg["base_url"] = base_url.rstrip("/")
        elif "deepseek" in model.lower():
            cfg["base_url"] = "https://api.deepseek.com"
            cfg["default"] = model or "deepseek-v4-flash"
        return cfg

    # No usable LLM key: avoid Hermes default openrouter (needs OPENROUTER_API_KEY).
    cfg: dict[str, str] = {"provider": "openai-api", "default": model or "gpt-4o-mini"}
    if base_url:
        cfg["base_url"] = base_url.rstrip("/")
    return cfg


def has_real_llm_key(ws_env: dict[str, str]) -> bool:
    for name in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        if is_real_api_key(ws_env.get(name)):
            return True
    return False


def _update_config_yaml(model_cfg: dict[str, str]) -> None:
    config_path = HERMES_HOME / "config.yaml"
    data: dict = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    agent = data.get("agent")
    if not isinstance(agent, dict):
        agent = {}
    agent.setdefault("max_turns", 100)
    data["agent"] = agent
    existing_model = data.get("model")
    if not isinstance(existing_model, dict):
        existing_model = {}
    data["model"] = {**existing_model, **model_cfg}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def bootstrap(*, force_key: bool = False) -> Path:
    ws_env = _parse_env(ROOT / ".env")
    existing = _parse_env(HERMES_ENV)

    api_key = (ws_env.get("HERMES_AGENT_API_KEY") or existing.get("API_SERVER_KEY") or "").strip()
    if not api_key or force_key:
        api_key = secrets.token_hex(32)

    hermes_updates = {
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": "8642",
        "API_SERVER_KEY": api_key,
        "WEB_SCRAPER_BASE_URL": ws_env.get("PUBLIC_BASE_URL", "http://127.0.0.1:8090"),
    }
    for src, dst in _LLM_ENV_SYNC:
        value = (ws_env.get(src) or "").strip()
        if value:
            hermes_updates[dst] = value

    _upsert_env(HERMES_ENV, hermes_updates)
    _update_config_yaml(resolve_llm_model_config(ws_env))

    ws_updates: dict[str, str] = {}
    if not (ws_env.get("HERMES_AGENT_API_KEY") or "").strip():
        ws_updates["HERMES_AGENT_API_KEY"] = api_key
    if not (ws_env.get("HERMES_AGENT_URL") or "").strip():
        ws_updates["HERMES_AGENT_URL"] = "http://127.0.0.1:8080"
    if not (ws_env.get("HERMES_AGENT_CHAT_URL") or "").strip():
        ws_updates["HERMES_AGENT_CHAT_URL"] = "http://127.0.0.1:8642"
    if ws_updates:
        _upsert_env(ROOT / ".env", ws_updates)

    return HERMES_ENV


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap Hermes Agent local config")
    parser.add_argument("--force-key", action="store_true", help="Regenerate API_SERVER_KEY")
    args = parser.parse_args()
    ws_env = _parse_env(ROOT / ".env")
    env_path = bootstrap(force_key=args.force_key)
    print(f"Hermes Agent config ready: {env_path}")
    model_cfg = resolve_llm_model_config(ws_env)
    print(f"  model.provider={model_cfg.get('provider')} model.default={model_cfg.get('default')}")
    if not has_real_llm_key(ws_env):
        print(
            "  WARNING: 未检测到有效 LLM API Key（OPENROUTER_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY）。"
            " Hermes 对话将返回 401 Missing Authentication header，请在 .env 填入真实 Key 后重新 bootstrap。"
        )


if __name__ == "__main__":
    main()
