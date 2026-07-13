"""商机线索 Agent 配置加载器（config/biz_clue.yaml 统一入口）。"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.timezone_utils import app_now

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
BIZ_CLUE_PATH = ROOT / "config" / "biz_clue.yaml"
KEYWORDS_PATH = ROOT / "config" / "biz_clue_keywords.yaml"
PROMPT_PATH = ROOT / "config" / "biz_clue_prompt.txt"
SOURCES_PATH = ROOT / "config" / "biz_clue_sources.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("加载 YAML 失败 %s: %s", path, exc)
        return {}


def _merge_keywords_fallback(cfg: dict[str, Any]) -> dict[str, Any]:
    """主配置缺字段时合并 biz_clue_keywords.yaml。"""
    legacy = _read_yaml(KEYWORDS_PATH)
    if not legacy:
        return cfg
    for key in (
        "product_lines",
        "stages",
        "exclude_keywords",
        "funding_keywords",
        "source_levels",
    ):
        if not cfg.get(key) and legacy.get(key):
            cfg[key] = legacy[key]
    return cfg


@lru_cache(maxsize=1)
def load_biz_clue_config() -> dict[str, Any]:
    """加载完整商机配置（缓存）。"""
    cfg = _read_yaml(BIZ_CLUE_PATH)
    if not cfg:
        cfg = _read_yaml(KEYWORDS_PATH)
    else:
        cfg = _merge_keywords_fallback(cfg)
    cfg.setdefault("version", "1.0")
    return cfg


def load_biz_clue_keywords() -> dict[str, Any]:
    """兼容旧接口：返回词库相关配置子集。"""
    cfg = load_biz_clue_config()
    return {
        "product_lines": cfg.get("product_lines") or {},
        "stages": cfg.get("stages") or [],
        "exclude_keywords": cfg.get("exclude_keywords") or [],
        "funding_keywords": cfg.get("funding_keywords") or {},
        "source_levels": _normalize_source_levels(cfg),
    }


def _normalize_source_levels(cfg: dict[str, Any]) -> dict[str, Any]:
    """统一 P0_domains / P0.domains 两种写法。"""
    raw = cfg.get("source_levels") or {}
    if raw.get("P0_domains") or raw.get("P1_domains"):
        return raw
    result: dict[str, Any] = {}
    for level in ("P0", "P1", "P2"):
        entry = raw.get(level) or {}
        if isinstance(entry, dict):
            domains = entry.get("domains") or []
            result[f"{level}_domains"] = list(domains)
        elif isinstance(entry, list):
            result[f"{level}_domains"] = list(entry)
    return result


def load_biz_clue_prompt() -> str:
    cfg = load_biz_clue_config()
    prompt_rel = cfg.get("prompt_file") or "config/biz_clue_prompt.txt"
    prompt_path = ROOT / prompt_rel if not Path(prompt_rel).is_absolute() else Path(prompt_rel)
    if prompt_path.is_file():
        try:
            return prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    if PROMPT_PATH.is_file():
        try:
            return PROMPT_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


@lru_cache(maxsize=1)
def load_biz_clue_sources() -> dict[str, Any]:
    sources_rel = load_biz_clue_config().get("sources_file") or "config/biz_clue_sources.yaml"
    sources_path = ROOT / sources_rel if not Path(sources_rel).is_absolute() else Path(sources_rel)
    cfg = _read_yaml(sources_path)
    if not cfg:
        cfg = _read_yaml(SOURCES_PATH)
    return cfg


def get_config_version() -> str:
    return str(load_biz_clue_config().get("version") or "1.0")


def get_product_lines() -> dict[str, Any]:
    return load_biz_clue_config().get("product_lines") or {}


def get_stages() -> list[dict[str, Any]]:
    return list(load_biz_clue_config().get("stages") or [])


def get_scoring_config() -> dict[str, Any]:
    return load_biz_clue_config().get("scoring") or {}


def get_scoring_levels() -> dict[str, int]:
    levels = get_scoring_config().get("levels") or {}
    return {
        "S": int(levels.get("S", 85)),
        "A": int(levels.get("A", 70)),
        "B": int(levels.get("B", 50)),
        "C": int(levels.get("C", 0)),
    }


def get_stage_by_name(name: str) -> Optional[dict[str, Any]]:
    for stage in get_stages():
        if stage.get("name") == name:
            return stage
    return None


def compute_next_check_date(stage_name: str, *, base_date: Optional[Any] = None) -> str:
    """按阶段复查周期计算 next_check_date。"""
    stage = get_stage_by_name(stage_name)
    check_days = int((stage or {}).get("check_days") or 7)
    ref = base_date or app_now()
    if hasattr(ref, "strftime"):
        from datetime import datetime

        if not isinstance(ref, datetime):
            ref = app_now()
        return (ref + timedelta(days=check_days)).strftime("%Y-%m-%d")
    return (app_now() + timedelta(days=check_days)).strftime("%Y-%m-%d")


def get_biz_clue_sync_site_ids() -> list[str]:
    sources = load_biz_clue_sources()
    ids = sources.get("default_sync_site_ids") or []
    if ids:
        return [str(i) for i in ids if str(i).strip()]
    merged: list[str] = []
    for level in ("P0", "P1"):
        entry = sources.get(level) or {}
        if isinstance(entry, dict):
            merged.extend(entry.get("site_ids") or [])
    return [str(i) for i in merged if str(i).strip()]


def get_biz_clue_search_keywords() -> list[str]:
    sources = load_biz_clue_sources()
    kws = list(sources.get("search_keywords") or [])
    cfg = load_biz_clue_config()
    kws.extend(cfg.get("long_tail_keywords") or [])
    seen: set[str] = set()
    result: list[str] = []
    for kw in kws:
        kw = str(kw).strip()
        if kw and kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result


def get_config_for_api() -> dict[str, Any]:
    """API 返回的完整配置视图。"""
    cfg = load_biz_clue_config()
    sources = load_biz_clue_sources()
    schedule_path = ROOT / "config" / "schedule.yaml"
    schedule_cfg = _read_yaml(schedule_path).get("scheduler") or {}
    return {
        "version": get_config_version(),
        "description": cfg.get("description") or "",
        "product_lines": get_product_lines(),
        "stages": get_stages(),
        "exclude_keywords": cfg.get("exclude_keywords") or [],
        "source_levels": cfg.get("source_levels") or {},
        "sources": sources,
        "search_templates": cfg.get("search_templates") or [],
        "long_tail_keywords": cfg.get("long_tail_keywords") or [],
        "scoring": get_scoring_config(),
        "validity_rules": cfg.get("validity_rules") or {},
        "dedup_rules": cfg.get("dedup_rules") or {},
        "human_review": cfg.get("human_review") or {},
        "acceptance_metrics": cfg.get("acceptance_metrics") or {},
        "workflow": cfg.get("workflow") or {},
        "schedule": {
            "daily_biz_clue_sync_enabled": schedule_cfg.get("daily_biz_clue_sync_enabled", True),
            "daily_biz_clue_sync_cron": schedule_cfg.get("daily_biz_clue_sync_cron", "30 6 * * *"),
            "daily_biz_clue_brief_cron": schedule_cfg.get("daily_biz_clue_brief_cron", "30 8 * * *"),
        },
        "prompt_preview": load_biz_clue_prompt()[:500],
    }


def clear_config_cache() -> None:
    load_biz_clue_config.cache_clear()
    load_biz_clue_sources.cache_clear()


__all__ = [
    "BIZ_CLUE_PATH",
    "clear_config_cache",
    "compute_next_check_date",
    "get_biz_clue_search_keywords",
    "get_biz_clue_sync_site_ids",
    "get_config_for_api",
    "get_config_version",
    "get_product_lines",
    "get_scoring_config",
    "get_scoring_levels",
    "get_stage_by_name",
    "get_stages",
    "load_biz_clue_config",
    "load_biz_clue_keywords",
    "load_biz_clue_prompt",
    "load_biz_clue_sources",
]
