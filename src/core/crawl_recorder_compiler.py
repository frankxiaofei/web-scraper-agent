"""Chrome 插件录制步骤 → 工作流图编译。"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.core.crawl_rule_workflow import (
    DEFAULT_NODE_DATA,
    _build_linear_edges,
    _make_node,
    rule_to_workflow,
)
from src.core.crawl_rules import CrawlRule

# 录制动作类型 → 工作流节点
RECORDING_ACTION_TYPES = (
    "navigate",
    "click",
    "input",
    "scroll",
    "wait",
    "mark_list_page",
)


def _step_to_node(step: dict[str, Any], index: int) -> Optional[dict[str, Any]]:
    """将单条录制步骤转为工作流节点（不含 meta / 列表 / 分页 / 详情 / save）。"""
    stype = step.get("type") or step.get("action")
    params = step.get("params") or step
    selector = step.get("selector") or params.get("selector") or ""

    if stype == "navigate":
        url = params.get("url") or step.get("url") or ""
        if url:
            return _make_node("navigate", {"url": url}, index=index)
        return None

    if stype == "click":
        text = params.get("text") or step.get("text") or ""
        data: dict[str, Any] = {"selector": selector, "text": "", "exact": False}
        if text and not selector:
            data = {"selector": "", "text": text, "exact": bool(params.get("exact"))}
        elif text:
            data["text"] = text
        if selector or text:
            return _make_node("click", data, index=index)
        return None

    if stype == "scroll":
        return _make_node(
            "scroll",
            {
                "direction": params.get("direction") or "down",
                "amount": int(params.get("amount") or 500),
            },
            index=index,
        )

    if stype == "wait":
        if selector:
            return _make_node(
                "wait",
                {"selector": selector, "timeout_ms": int(params.get("timeout_ms") or 15000)},
                index=index,
            )
        return _make_node("wait", {"timeout_ms": int(params.get("timeout_ms") or 15000)}, index=index)

    if stype in ("input", "fill"):
        if selector:
            return _make_node(
                "wait",
                {"selector": selector, "timeout_ms": int(params.get("timeout_ms") or 15000)},
                index=index,
            )
        return _make_node("wait", {"timeout_ms": 500}, index=index)

    return None


def suggest_pagination_from_html(html: str) -> dict[str, Any]:
    """从 HTML 启发式推断分页选择器。"""
    text = html or ""
    candidates: list[tuple[int, dict[str, Any]]] = []

    next_class = re.search(
        r'<a[^>]+class=["\'][^"\']*\bnext\b[^"\']*["\'][^>]*>',
        text,
        re.I,
    )
    if next_class:
        m = re.search(r'class=["\']([^"\']+)["\']', next_class.group(0), re.I)
        if m:
            cls = m.group(1).split()[0]
            candidates.append((30, {"type": "next_button", "selector": f".{cls}"}))

    for label in ("下一页", "下页", "Next", "next"):
        pat = rf'<a[^>]*>\s*{re.escape(label)}\s*</a>'
        if re.search(pat, text, re.I):
            candidates.append(
                (
                    25,
                    {
                        "type": "next_button",
                        "selector": ".pagination a, .pager a, a.next",
                    },
                )
            )
            break

    page_num = re.search(
        r'<(?:a|span)[^>]+class=["\'][^"\']*(?:pagination|pager|page)[^"\']*["\']',
        text,
        re.I,
    )
    if page_num:
        candidates.append(
            (
                15,
                {"type": "page_number", "selector": ".pagination a, .pager a"},
            )
        )

    if not candidates:
        return dict(DEFAULT_NODE_DATA["paginate"])

    _, best = max(candidates, key=lambda x: x[0])
    merged = dict(DEFAULT_NODE_DATA["paginate"])
    merged.update(best)
    return merged


def suggest_detail_from_list_analysis(analysis: Optional[dict[str, Any]]) -> dict[str, Any]:
    """根据列表分析生成详情节点默认参数。"""
    detail = dict(DEFAULT_NODE_DATA["extract_detail"])
    detail["fetch_detail"] = True
    selectors = (analysis or {}).get("selectors") or {}
    title_sel = selectors.get("title") or "h1, .title, .article-title"
    detail["title_selector"] = title_sel
    detail["content_selector"] = ".content, .article-content, #content, .detail, main"
    return detail


def compile_recorded_workflow(
    *,
    site_id: str,
    site_name: str = "",
    entry_url: str = "",
    steps: list[dict[str, Any]],
    list_analysis: Optional[dict[str, Any]] = None,
    html_snapshot: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    """将录制步骤 + 列表页分析编译为工作流 nodes/edges。"""
    rule_parts: dict[str, Any] = {
        "site_id": site_id,
        "name": site_name or site_id,
        "enabled": enabled,
        "version": 1,
    }

    if entry_url:
        rule_parts["entry_url"] = entry_url

    entry_steps: list[dict[str, Any]] = []
    first_navigate_used = False

    for step in steps:
        stype = step.get("type") or step.get("action")
        if stype == "mark_list_page":
            continue
        node = _step_to_node(step, index=0)
        if not node:
            continue
        if node["type"] == "navigate":
            url = (node.get("data") or {}).get("url") or ""
            if url and not first_navigate_used and not rule_parts.get("entry_url"):
                rule_parts["entry_url"] = url
                first_navigate_used = True
            elif url and url != rule_parts.get("entry_url"):
                entry_steps.append({"action": "navigate", "url": url})
            elif url:
                first_navigate_used = True
            continue

        from src.core.crawl_rule_workflow import _node_to_entry_step

        entry_step = _node_to_entry_step(node)
        if entry_step:
            entry_steps.append(entry_step)

    if entry_steps:
        rule_parts["entry_steps"] = entry_steps

    selectors = (list_analysis or {}).get("selectors") or {}
    if selectors:
        list_page = {
            "strategy": selectors.get("strategy") or "dom",
            "container": selectors.get("container") or "",
            "item": selectors.get("item") or "",
            "title": selectors.get("title") or "a",
            "link": selectors.get("link") or "a",
        }
        if selectors.get("date"):
            list_page["date"] = selectors["date"]
        if selectors.get("wait_for"):
            list_page["wait_for"] = selectors["wait_for"]
        rule_parts["list_page"] = list_page
    else:
        rule_parts["list_page"] = {"strategy": "dom"}

    pagination = suggest_pagination_from_html(html_snapshot)
    if list_analysis and list_analysis.get("pagination"):
        pagination.update(list_analysis["pagination"])
    rule_parts["pagination"] = pagination

    rule_parts["detail"] = suggest_detail_from_list_analysis(list_analysis)
    rule_parts["limits"] = dict(DEFAULT_NODE_DATA["save"])

    rule = CrawlRule.model_validate(rule_parts)
    return rule_to_workflow(rule)
