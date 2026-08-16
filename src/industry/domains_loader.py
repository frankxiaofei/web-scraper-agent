"""加载 config/industry/domains.yaml。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
DOMAINS_PATH = ROOT / "config" / "industry" / "domains.yaml"


@lru_cache(maxsize=1)
def load_industry_domains() -> dict[str, Any]:
    if not DOMAINS_PATH.is_file():
        return {"version": 1, "default_domain": "数字农业", "domains": {}}
    with DOMAINS_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "domains" not in data:
        data["domains"] = {}
    return data


def get_domain_config(domain: str) -> dict[str, Any] | None:
    return load_industry_domains().get("domains", {}).get(domain)


def list_domain_names() -> list[str]:
    return list(load_industry_domains().get("domains", {}).keys())


def default_domain() -> str:
    return str(load_industry_domains().get("default_domain") or "数字农业")
