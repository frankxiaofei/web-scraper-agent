"""Crawl Agent 动作审计：JSONL 追加写入（data/crawl_agent_actions.jsonl）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from src.core.config import get_settings

logger = logging.getLogger(__name__)

ActionSource = Literal["hermes", "web", "daemon"]
VALID_SOURCES: frozenset[str] = frozenset({"hermes", "web", "daemon"})

SOURCE_LABELS: dict[str, str] = {
    "hermes": "Hermes",
    "web": "Web",
    "daemon": "守护进程",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_path() -> Path:
    return get_settings().data_dir / "crawl_agent_actions.jsonl"


def _load_all_records() -> list[dict[str, Any]]:
    path = _store_path()
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    records.append(rec)
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        logger.warning("读取 agent actions 失败: %s", exc)
    return records


def has_action(*, site_id: str, action: str, detail_contains: str) -> bool:
    """检查是否已有匹配的动作记录（用于 pending resolve 等去重）。"""
    needle = detail_contains.strip()
    if not needle:
        return False
    for rec in _load_all_records():
        if rec.get("site_id") != site_id:
            continue
        if rec.get("action") != action:
            continue
        if needle in (rec.get("detail") or ""):
            return True
    return False


def append_action(
    *,
    site_id: str,
    action: str,
    detail: str = "",
    source: ActionSource = "web",
) -> dict[str, Any]:
    if source not in VALID_SOURCES:
        raise ValueError(f"不支持的 source: {source}")
    if not site_id.strip():
        raise ValueError("site_id 不能为空")
    if not action.strip():
        raise ValueError("action 不能为空")

    record: dict[str, Any] = {
        "timestamp": _utcnow_iso(),
        "site_id": site_id.strip(),
        "action": action.strip(),
        "detail": (detail or "").strip(),
        "source": source,
    }
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def append_action_if_absent(
    *,
    site_id: str,
    action: str,
    detail: str = "",
    source: ActionSource = "web",
    dedup_detail_contains: str,
) -> Optional[dict[str, Any]]:
    """追加动作；若同 site_id + action + detail 片段已存在则跳过（返回 None）。"""
    if has_action(site_id=site_id, action=action, detail_contains=dedup_detail_contains):
        return None
    return append_action(
        site_id=site_id,
        action=action,
        detail=detail,
        source=source,
    )


def list_actions(*, site_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    if limit < 1:
        return []
    records = _load_all_records()
    if site_id:
        records = [r for r in records if r.get("site_id") == site_id]
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return records[:limit]


def recent_by_site(*, per_site: int = 3) -> dict[str, list[dict[str, Any]]]:
    """按站点分组，每站最多 per_site 条（已按时间倒序）。"""
    if per_site < 1:
        return {}
    records = _load_all_records()
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    by_site: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        sid = rec.get("site_id") or ""
        if not sid:
            continue
        bucket = by_site.setdefault(sid, [])
        if len(bucket) < per_site:
            bucket.append(rec)
    return by_site


def format_timestamp_fmt(value: Any) -> str:
    if not value or not isinstance(value, str):
        return "—"
    text = value.replace("T", " ").split("+")[0].split(".")[0]
    return text or "—"


def format_action_summary(rec: dict[str, Any]) -> str:
    source = SOURCE_LABELS.get(rec.get("source", ""), rec.get("source", ""))
    action = rec.get("action", "")
    detail = (rec.get("detail") or "").strip()
    if len(detail) > 60:
        detail = detail[:57] + "..."
    parts = [p for p in (source, action, detail) if p]
    return " · ".join(parts)


def enrich_action(rec: dict[str, Any]) -> dict[str, Any]:
    row = dict(rec)
    row["summary"] = format_action_summary(rec)
    row["timestamp_fmt"] = format_timestamp_fmt(rec.get("timestamp"))
    row["source_label"] = SOURCE_LABELS.get(rec.get("source", ""), rec.get("source", ""))
    return row
