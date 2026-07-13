"""浏览器录制 JSON → CrawlRule YAML（stub 直转 + LLM 上下文格式化）。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import yaml


def _get_site_by_id(site_id: str):
    from src.core.site_sync import get_site_by_id

    return get_site_by_id(site_id)


def format_recording_for_llm(recording: dict[str, Any]) -> str:
    """将录制 JSON 格式化为 LLM 可读上下文。"""
    lines = [
        f"site_id: {recording.get('site_id', '')}",
        f"entry_url: {recording.get('entry_url', '')}",
        f"recorded_at: {recording.get('recorded_at', '')}",
        "",
        "录制步骤:",
    ]
    for i, step in enumerate(recording.get("steps") or [], 1):
        parts = [f"  {i}. action={step.get('action')}"]
        for key in ("url", "selector", "text", "tag", "url_pattern", "note"):
            if step.get(key):
                parts.append(f"{key}={step[key]!r}")
        lines.append(" ".join(parts))

    hints = recording.get("hints") or {}
    if hints:
        lines.append("")
        lines.append("页面提示 (hints):")
        if hints.get("list_container"):
            lines.append(f"  list_container: {hints['list_container']}")
        if hints.get("sample_links"):
            lines.append("  sample_links:")
            for link in hints["sample_links"][:10]:
                lines.append(f"    - {link}")

    return "\n".join(lines)


def _step_to_entry(step: dict[str, Any]) -> Optional[dict[str, Any]]:
    action = step.get("action")
    if action == "navigate":
        url = step.get("url")
        if url:
            return {"action": "navigate", "url": url}
    elif action == "click":
        selector = step.get("selector")
        text = (step.get("text") or "").strip()
        if text and len(text) <= 40:
            return {"action": "click_text", "text": text}
        if selector:
            return {"action": "click_selector", "selector": selector}
    elif action == "wait_network":
        pattern = step.get("url_pattern")
        if pattern:
            return {"action": "wait_network", "url_pattern": pattern, "timeout_ms": 15000}
    elif action == "pick_selector":
        selector = step.get("selector")
        if selector:
            return {"action": "wait", "selector": selector, "timeout_ms": 15000}
    return None


def _detect_pagination(steps: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for step in reversed(steps):
        if step.get("action") == "click" and step.get("note") == "pagination":
            sel = step.get("selector")
            if sel:
                return {
                    "type": "next_button",
                    "selector": sel,
                    "max_pages": 20,
                    "stop_when_empty": True,
                }
        text = (step.get("text") or "").strip()
        if step.get("action") == "click" and text in ("下一页", "下页", ">", "»"):
            sel = step.get("selector")
            if sel:
                return {
                    "type": "next_button",
                    "selector": sel,
                    "max_pages": 20,
                    "stop_when_empty": True,
                }
    return None


def _detect_wait_network(steps: list[dict[str, Any]]) -> Optional[str]:
    for step in steps:
        if step.get("action") == "wait_network" and step.get("url_pattern"):
            return step["url_pattern"]
    return None


def _infer_list_page(
    hints: dict[str, Any],
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    container = hints.get("list_container") or "#list, .list, ul"
    wait_pattern = _detect_wait_network(steps)
    list_page: dict[str, Any] = {
        "strategy": "dom_after_ajax" if wait_pattern else "dom",
        "container": container,
        "item": "li",
        "title": "a",
        "link": "a",
    }
    if wait_pattern:
        list_page["wait_network"] = {"url_pattern": wait_pattern, "timeout_ms": 15000}
    return list_page


def recording_to_yaml_stub(
    recording: dict[str, Any],
    *,
    site_id: str,
    site_name: Optional[str] = None,
) -> str:
    """无 LLM 时将录制 JSON 直转为最小可用 CrawlRule YAML。"""
    site = _get_site_by_id(site_id)
    name = site_name or (site or {}).get("name") or site_id
    entry_url = recording.get("entry_url") or (site or {}).get("url") or ""

    steps = recording.get("steps") or []
    hints = recording.get("hints") or {}

    entry_steps: list[dict[str, Any]] = []
    for step in steps:
        if step.get("note") == "pagination":
            continue
        converted = _step_to_entry(step)
        if converted and converted.get("action") != "navigate":
            entry_steps.append(converted)

    # 首步 navigate 通常作为 entry_url，不重复写入 entry_steps
    entry_steps = [s for s in entry_steps if s.get("action") != "navigate"]

    rule: dict[str, Any] = {
        "version": 1,
        "site_id": site_id,
        "name": name,
        "enabled": True,
        "entry_url": entry_url,
        "entry_steps": entry_steps,
        "list_page": _infer_list_page(hints, steps),
        "detail": {"fetch_detail": False},
        "limits": {
            "max_pages": 20,
            "max_items": 100,
            "max_depth": 2,
            "rate_limit_seconds": 1.0,
        },
    }

    pagination = _detect_pagination(steps)
    if pagination:
        rule["pagination"] = pagination

    return yaml.dump(rule, allow_unicode=True, default_flow_style=False, sort_keys=False)


def parse_recording_json(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, str):
        return json.loads(raw)
    return raw
