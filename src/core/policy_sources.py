"""policy_sources.yaml 配置加载。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
POLICY_SOURCES_PATH = ROOT / "config" / "policy_sources.yaml"


@lru_cache(maxsize=1)
def load_policy_sources_config() -> dict[str, Any]:
    if not POLICY_SOURCES_PATH.exists():
        return {}
    with POLICY_SOURCES_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def get_policy_match_keywords() -> list[str]:
    cfg = load_policy_sources_config()
    return [str(k).strip() for k in (cfg.get("match_keywords") or []) if str(k).strip()]


def get_policy_themes() -> list[dict[str, Any]]:
    cfg = load_policy_sources_config()
    return [t for t in (cfg.get("themes") or []) if isinstance(t, dict)]


def get_policy_scope_label() -> str:
    cfg = load_policy_sources_config()
    return str(cfg.get("scope_label") or "国内政府权威网站")


def get_policy_mock_items() -> list[dict[str, Any]]:
    cfg = load_policy_sources_config()
    return [i for i in (cfg.get("mock_items") or []) if isinstance(i, dict)]


def use_policy_mock_when_empty() -> bool:
    env_raw = os.getenv("POLICY_USE_MOCK_WHEN_EMPTY", "").strip().lower()
    if env_raw in ("1", "true", "yes", "on"):
        return True
    if env_raw in ("0", "false", "no", "off"):
        return False
    cfg = load_policy_sources_config()
    return bool(cfg.get("use_mock_when_empty", False))


def matched_policy_themes(text: str) -> list[str]:
    if not text.strip():
        return []
    found: list[str] = []
    for theme in get_policy_themes():
        name = str(theme.get("name") or theme.get("id") or "")
        for kw in theme.get("keywords") or []:
            kw_str = str(kw).strip()
            if kw_str and kw_str in text:
                if name and name not in found:
                    found.append(name)
                break
    return found


def matched_policy_keywords(text: str) -> list[str]:
    if not text.strip():
        return []
    return [kw for kw in get_policy_match_keywords() if kw in text]
