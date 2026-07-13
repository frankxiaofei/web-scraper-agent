"""爬取规则 ↔ 工作流节点双向转换（浏览器动作节点 + YAML 编译）。"""

from __future__ import annotations

import uuid
from typing import Any, Optional

import yaml
from pydantic import ValidationError

from src.core.crawl_rules import CrawlRule

# 浏览器动作节点类型
ACTION_NODE_TYPES = (
    "meta",
    "navigate",
    "wait",
    "click",
    "scroll",
    "extract_list",
    "paginate",
    "extract_detail",
    "save",
)

# 旧版语义节点（向后兼容）
LEGACY_NODE_TYPES = (
    "entry",
    "entry_steps",
    "list_page",
    "pagination",
    "detail",
    "limits",
)

NODE_LABELS: dict[str, str] = {
    "meta": "站点元信息",
    "navigate": "打开 URL",
    "wait": "等待元素",
    "click": "点击元素",
    "scroll": "滚动页面",
    "extract_list": "列表提取",
    "paginate": "翻页",
    "extract_detail": "详情抓取",
    "save": "采集限制",
    # legacy
    "entry": "入口 URL",
    "entry_steps": "入口导航",
    "list_page": "列表页",
    "pagination": "分页",
    "detail": "详情抓取",
    "limits": "采集限制",
}

PALETTE_NODE_TYPES = [
    "navigate",
    "wait",
    "click",
    "scroll",
    "extract_list",
    "paginate",
    "extract_detail",
    "save",
]

DEFAULT_NODE_DATA: dict[str, dict[str, Any]] = {
    "meta": {"version": 1, "enabled": True},
    "navigate": {"url": ""},
    "wait": {"selector": "", "timeout_ms": 15000},
    "click": {"selector": "", "text": "", "exact": False},
    "scroll": {"direction": "down", "amount": 500},
    "extract_list": {
        "strategy": "dom",
        "container": "",
        "item": "",
        "title": "",
        "link": "",
        "date": "",
        "wait_for": "",
    },
    "paginate": {
        "type": "next_button",
        "selector": "",
        "max_pages": 20,
        "stop_when_empty": True,
    },
    "extract_detail": {
        "fetch_detail": False,
        "strategy": "dom",
        "content_selector": "",
        "title_selector": "",
    },
    "save": {
        "max_pages": 20,
        "max_items": 100,
        "max_depth": 2,
        "rate_limit_seconds": 0.0,
    },
}


def _new_id(node_type: str) -> str:
    return f"{node_type}_{uuid.uuid4().hex[:8]}"


def _default_position(index: int) -> dict[str, int]:
    """水平流向默认排布：每行最多 4 个节点，便于画布首屏展示。"""
    col_w, row_h, cols = 220, 120, 4
    row, col = divmod(index, cols)
    return {"x": 48 + col * col_w, "y": 48 + row * row_h}


def _node_summary(node_type: str, data: dict[str, Any]) -> str:
    if node_type == "meta":
        flag = "启用" if data.get("enabled", True) else "禁用"
        return f"{data.get('name') or data.get('site_id', '—')} · {flag}"
    if node_type == "navigate":
        return (data.get("url") or "")[:60] or "未设置 URL"
    if node_type == "wait":
        sel = data.get("selector") or data.get("url_pattern") or ""
        return f"等待 {sel[:40]}" if sel else f"超时 {data.get('timeout_ms', 15000)}ms"
    if node_type == "click":
        return (data.get("selector") or data.get("text") or "—")[:40]
    if node_type == "scroll":
        return f"{data.get('direction', 'down')} · {data.get('amount', 500)}px"
    if node_type == "extract_list":
        strategy = data.get("strategy") or "dom"
        if strategy == "api":
            api = data.get("api") or {}
            return f"API · {(api.get('url') or '')[:40]}"
        return f"{strategy} · {data.get('container') or data.get('item') or '—'}"
    if node_type == "paginate":
        ptype = data.get("type") or "next_button"
        max_pages = data.get("max_pages")
        return f"{ptype}" + (f" · 最多 {max_pages} 页" if max_pages else "")
    if node_type == "extract_detail":
        return "抓取详情" if data.get("fetch_detail") else "仅列表"
    if node_type == "save":
        return f"最多 {data.get('max_items', 100)} 条 · {data.get('max_pages', 20)} 页"
    # legacy
    if node_type == "entry":
        return (data.get("entry_url") or "")[:60] or "未设置"
    if node_type == "entry_steps":
        steps = data.get("entry_steps") or []
        return f"{len(steps)} 步" if steps else "无导航步骤"
    if node_type == "list_page":
        return _node_summary("extract_list", data)
    if node_type == "pagination":
        return _node_summary("paginate", data)
    if node_type == "detail":
        return _node_summary("extract_detail", data)
    if node_type == "limits":
        return _node_summary("save", data)
    return ""


def _make_node(
    node_type: str,
    data: dict[str, Any],
    *,
    node_id: Optional[str] = None,
    index: int = 0,
) -> dict[str, Any]:
    return {
        "id": node_id or _new_id(node_type),
        "type": node_type,
        "label": NODE_LABELS.get(node_type, node_type),
        "summary": _node_summary(node_type, data),
        "position": _default_position(index),
        "data": data,
    }


def _build_linear_edges(node_ids: list[str]) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    for i in range(len(node_ids) - 1):
        edges.append({"from": node_ids[i], "to": node_ids[i + 1]})
    return edges


def _entry_step_to_node(step: dict[str, Any], index: int) -> dict[str, Any]:
    action = step.get("action") or "wait"
    if action == "navigate":
        return _make_node("navigate", {"url": step.get("url") or ""}, index=index)
    if action == "click_selector":
        return _make_node(
            "click",
            {"selector": step.get("selector") or "", "text": "", "exact": False},
            index=index,
        )
    if action == "click_text":
        return _make_node(
            "click",
            {"text": step.get("text") or "", "selector": "", "exact": bool(step.get("exact"))},
            index=index,
        )
    if action == "wait_network":
        return _make_node(
            "wait",
            {
                "url_pattern": step.get("url_pattern") or "",
                "timeout_ms": step.get("timeout_ms", 15000),
                "mode": "network",
            },
            index=index,
        )
    if action == "follow_link":
        return _make_node(
            "click",
            {
                "text": step.get("text") or "",
                "href_contains": step.get("href_contains") or "",
            },
            index=index,
        )
    return _make_node(
        "wait",
        {"selector": step.get("selector") or "", "timeout_ms": step.get("timeout_ms", 15000)},
        index=index,
    )


def _node_to_entry_step(node: dict[str, Any]) -> Optional[dict[str, Any]]:
    ntype = node.get("type")
    data = node.get("data") or {}
    if ntype == "navigate":
        url = data.get("url")
        if url:
            return {"action": "navigate", "url": url}
        return None
    if ntype == "click":
        if data.get("selector"):
            return {"action": "click_selector", "selector": data["selector"]}
        if data.get("text"):
            step: dict[str, Any] = {"action": "click_text", "text": data["text"]}
            if data.get("exact"):
                step["exact"] = True
            return step
        if data.get("href_contains"):
            return {
                "action": "follow_link",
                "href_contains": data["href_contains"],
                "text": data.get("text"),
            }
        return None
    if ntype == "wait":
        if data.get("mode") == "network" or data.get("url_pattern"):
            return {
                "action": "wait_network",
                "url_pattern": data.get("url_pattern") or "",
                "timeout_ms": data.get("timeout_ms", 15000),
            }
        if data.get("selector"):
            return {
                "action": "wait",
                "selector": data["selector"],
                "timeout_ms": data.get("timeout_ms", 15000),
            }
        return {"action": "wait", "timeout_ms": data.get("timeout_ms", 15000)}
    if ntype == "scroll":
        return {"action": "wait", "timeout_ms": 300}
    return None


def _is_legacy_workflow(workflow: dict[str, Any]) -> bool:
    for node in workflow.get("nodes") or []:
        if node.get("type") in LEGACY_NODE_TYPES:
            return True
    return False


def _sort_nodes_linear(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    if not nodes:
        return []

    by_id = {n["id"]: n for n in nodes if n.get("id")}
    if not edges:
        return sorted(
            nodes,
            key=lambda n: (
                (n.get("position") or {}).get("y", 0),
                (n.get("position") or {}).get("x", 0),
            ),
        )

    next_map: dict[str, str] = {}
    incoming: set[str] = set()
    for edge in edges:
        src, dst = edge.get("from"), edge.get("to")
        if src and dst:
            next_map[src] = dst
            incoming.add(dst)

    start_id: Optional[str] = None
    for node in nodes:
        nid = node.get("id")
        if nid and nid not in incoming:
            start_id = nid
            break
    if not start_id:
        meta = next((n for n in nodes if n.get("type") == "meta"), None)
        start_id = meta["id"] if meta else nodes[0].get("id")

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur: Optional[str] = start_id
    while cur and cur not in seen and cur in by_id:
        seen.add(cur)
        ordered.append(by_id[cur])
        cur = next_map.get(cur)

    for node in nodes:
        if node.get("id") not in seen:
            ordered.append(node)
    return ordered


def rule_to_workflow(rule: CrawlRule | dict[str, Any]) -> dict[str, Any]:
    """将 CrawlRule 转为浏览器动作工作流 nodes/edges。"""
    if isinstance(rule, CrawlRule):
        raw = rule.model_dump(mode="json", exclude_none=False)
    else:
        raw = dict(rule)

    nodes: list[dict[str, Any]] = []
    node_ids: list[str] = []
    idx = 0

    meta_data = {
        "version": raw.get("version", 1),
        "site_id": raw.get("site_id", ""),
        "name": raw.get("name", ""),
        "enabled": raw.get("enabled", True),
    }
    meta = _make_node("meta", meta_data, node_id="meta", index=idx)
    nodes.append(meta)
    node_ids.append(meta["id"])
    idx += 1

    entry_url = raw.get("entry_url")
    if entry_url:
        nav = _make_node("navigate", {"url": entry_url}, index=idx)
        nodes.append(nav)
        node_ids.append(nav["id"])
        idx += 1

    for step in raw.get("entry_steps") or []:
        if isinstance(step, dict):
            step_node = _entry_step_to_node(step, idx)
            nodes.append(step_node)
            node_ids.append(step_node["id"])
            idx += 1

    list_page = raw.get("list_page")
    if list_page:
        lp = _make_node("extract_list", dict(list_page), index=idx)
        nodes.append(lp)
        node_ids.append(lp["id"])
        idx += 1

    pagination = raw.get("pagination")
    if pagination:
        pg = _make_node("paginate", dict(pagination), index=idx)
        nodes.append(pg)
        node_ids.append(pg["id"])
        idx += 1
    elif list_page:
        pg = _make_node("paginate", dict(DEFAULT_NODE_DATA["paginate"]), index=idx)
        nodes.append(pg)
        node_ids.append(pg["id"])
        idx += 1

    detail = raw.get("detail")
    if detail:
        dt = _make_node("extract_detail", dict(detail), index=idx)
        nodes.append(dt)
        node_ids.append(dt["id"])
        idx += 1
    else:
        dt = _make_node("extract_detail", dict(DEFAULT_NODE_DATA["extract_detail"]), index=idx)
        nodes.append(dt)
        node_ids.append(dt["id"])
        idx += 1

    limits = raw.get("limits") or DEFAULT_NODE_DATA["save"]
    sv = _make_node("save", dict(limits), index=idx)
    nodes.append(sv)
    node_ids.append(sv["id"])

    return {"nodes": nodes, "edges": _build_linear_edges(node_ids)}


yaml_to_workflow = rule_to_workflow


def _legacy_workflow_to_rule_dict(workflow: dict[str, Any], *, site_id: str) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    for node in workflow.get("nodes") or []:
        nid = node.get("id") or node.get("type")
        if nid:
            by_id[str(nid)] = node.get("data") or {}

    meta = by_id.get("meta", {})
    entry = by_id.get("entry", {})
    entry_steps = by_id.get("entry_steps", {})
    list_page = by_id.get("list_page", {})
    pagination = by_id.get("pagination", {})
    detail = by_id.get("detail", {})
    limits = by_id.get("limits", {})

    rule: dict[str, Any] = {
        "version": meta.get("version", 1),
        "site_id": site_id,
        "name": meta.get("name") or "",
        "enabled": meta.get("enabled", True),
    }
    if entry.get("entry_url"):
        rule["entry_url"] = entry["entry_url"]
    steps = entry_steps.get("entry_steps")
    if steps:
        rule["entry_steps"] = steps
    cleaned_list = _clean_dict(list_page)
    if cleaned_list:
        rule["list_page"] = cleaned_list
    cleaned_pagination = _clean_dict(pagination)
    if cleaned_pagination:
        rule["pagination"] = cleaned_pagination
    cleaned_detail = _clean_dict(detail)
    if cleaned_detail:
        rule["detail"] = cleaned_detail
    cleaned_limits = _clean_dict(limits)
    if cleaned_limits:
        rule["limits"] = cleaned_limits
    return rule


def _action_workflow_to_rule_dict(workflow: dict[str, Any], *, site_id: str) -> dict[str, Any]:
    ordered = _sort_nodes_linear(workflow)
    rule: dict[str, Any] = {
        "version": 1,
        "site_id": site_id,
        "name": "",
        "enabled": True,
    }
    entry_url_set = False
    entry_steps: list[dict[str, Any]] = []

    for node in ordered:
        ntype = node.get("type")
        data = dict(node.get("data") or {})

        if ntype == "meta":
            rule["version"] = data.get("version", 1)
            rule["name"] = data.get("name") or rule.get("name", "")
            rule["enabled"] = data.get("enabled", True)
            continue

        if ntype == "navigate":
            url = data.get("url")
            if url and not entry_url_set:
                rule["entry_url"] = url
                entry_url_set = True
            elif url:
                entry_steps.append({"action": "navigate", "url": url})
            continue

        if ntype in ("wait", "click", "scroll"):
            step = _node_to_entry_step(node)
            if step:
                entry_steps.append(step)
            continue

        if ntype == "extract_list":
            cleaned = _clean_dict(data)
            if cleaned:
                rule["list_page"] = cleaned
            continue

        if ntype == "paginate":
            cleaned = _clean_dict(data)
            if cleaned:
                rule["pagination"] = cleaned
            continue

        if ntype == "extract_detail":
            cleaned = _clean_dict(data)
            if cleaned:
                rule["detail"] = cleaned
            continue

        if ntype == "save":
            cleaned = _clean_dict(data)
            if cleaned:
                rule["limits"] = cleaned

    if entry_steps:
        rule["entry_steps"] = entry_steps
    return rule


def workflow_to_rule_dict(workflow: dict[str, Any], *, site_id: str) -> dict[str, Any]:
    """工作流 nodes 合并为 CrawlRule 字典。"""
    if _is_legacy_workflow(workflow):
        return _legacy_workflow_to_rule_dict(workflow, site_id=site_id)
    return _action_workflow_to_rule_dict(workflow, site_id=site_id)


def workflow_to_yaml(workflow: dict[str, Any], *, site_id: str) -> str:
    rule_dict = workflow_to_rule_dict(workflow, site_id=site_id)
    return yaml.safe_dump(rule_dict, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _clean_dict(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, dict):
            nested = _clean_dict(value)
            if nested:
                out[key] = nested
        elif isinstance(value, list):
            if value:
                out[key] = value
        elif value == "" and key not in ("date",):
            continue
        else:
            out[key] = value
    return out


def parse_workflow_payload(payload: dict[str, Any], *, site_id: str) -> tuple[Optional[CrawlRule], list[str]]:
    errors: list[str] = []
    try:
        rule_dict = workflow_to_rule_dict(payload, site_id=site_id)
        rule = CrawlRule.model_validate(rule_dict)
        if rule.site_id != site_id:
            errors.append(f"site_id 不匹配: 期望 {site_id}，实际 {rule.site_id}")
        return (rule if not errors else None), errors
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            errors.append(f"{loc}: {err.get('msg', '校验失败')}")
        return None, errors


def create_palette_node(node_type: str, *, index: int = 0) -> dict[str, Any]:
    """创建可拖入画布的新节点。"""
    data = dict(DEFAULT_NODE_DATA.get(node_type, {}))
    return _make_node(node_type, data, index=index)


NODE_FIELD_SCHEMAS: dict[str, list[dict[str, Any]]] = {
    "meta": [
        {"key": "name", "label": "站点名称", "type": "text"},
        {"key": "enabled", "label": "规则启用", "type": "checkbox"},
    ],
    "navigate": [
        {"key": "url", "label": "目标 URL", "type": "url", "placeholder": "https://..."},
    ],
    "wait": [
        {"key": "selector", "label": "等待选择器", "type": "text"},
        {"key": "url_pattern", "label": "网络 URL 模式", "type": "text"},
        {
            "key": "mode",
            "label": "等待模式",
            "type": "select",
            "options": ["selector", "network"],
        },
        {"key": "timeout_ms", "label": "超时 (ms)", "type": "number"},
    ],
    "click": [
        {"key": "selector", "label": "CSS 选择器", "type": "text"},
        {"key": "text", "label": "按钮/链接文字", "type": "text"},
        {"key": "exact", "label": "精确匹配文字", "type": "checkbox"},
        {"key": "href_contains", "label": "链接包含", "type": "text"},
    ],
    "scroll": [
        {
            "key": "direction",
            "label": "方向",
            "type": "select",
            "options": ["down", "up", "bottom"],
        },
        {"key": "amount", "label": "滚动像素", "type": "number"},
    ],
    "extract_list": [
        {"key": "strategy", "label": "策略", "type": "select", "options": ["dom", "api", "dom_after_ajax"]},
        {"key": "container", "label": "列表容器选择器", "type": "text"},
        {"key": "item", "label": "条目选择器", "type": "text"},
        {"key": "title", "label": "标题选择器", "type": "text"},
        {"key": "link", "label": "链接选择器", "type": "text"},
        {"key": "date", "label": "日期选择器", "type": "text"},
        {"key": "wait_for", "label": "等待选择器", "type": "text"},
        {
            "key": "api",
            "label": "API 配置 (JSON)",
            "type": "json",
            "show_when": {"strategy": ["api", "dom_after_ajax"]},
        },
    ],
    "paginate": [
        {
            "key": "type",
            "label": "分页类型",
            "type": "select",
            "options": ["next_button", "page_number", "load_more", "ajax_param"],
        },
        {"key": "selector", "label": "下一页选择器", "type": "text"},
        {"key": "disabled_selector", "label": "禁用态选择器", "type": "text"},
        {"key": "page_param", "label": "页码参数名", "type": "text"},
        {"key": "url_style", "label": "URL 风格", "type": "select", "options": ["query", "path_suffix"]},
        {"key": "page_size", "label": "每页条数", "type": "number"},
        {"key": "max_pages", "label": "最大页数", "type": "number"},
        {"key": "stop_when_empty", "label": "空页停止", "type": "checkbox"},
        {"key": "wait_after_ms", "label": "翻页后等待 (ms)", "type": "number"},
    ],
    "extract_detail": [
        {"key": "fetch_detail", "label": "抓取详情页", "type": "checkbox"},
        {"key": "strategy", "label": "策略", "type": "select", "options": ["dom", "api"]},
        {"key": "url_pattern", "label": "URL 匹配模式", "type": "text"},
        {"key": "content_selector", "label": "正文选择器", "type": "text"},
        {"key": "title_selector", "label": "标题选择器", "type": "text"},
        {"key": "wait_for", "label": "等待选择器", "type": "text"},
        {"key": "api_base_url", "label": "详情 API 基址", "type": "text"},
        {"key": "api_path_template", "label": "详情 API 路径模板", "type": "text"},
    ],
    "save": [
        {"key": "max_pages", "label": "最大页数", "type": "number"},
        {"key": "max_items", "label": "最大条数", "type": "number"},
        {"key": "max_depth", "label": "最大深度", "type": "number"},
        {"key": "rate_limit_seconds", "label": "请求间隔 (秒)", "type": "number", "step": 0.1},
    ],
    # legacy aliases
    "entry": [
        {"key": "entry_url", "label": "入口 URL", "type": "url", "placeholder": "https://..."},
    ],
    "entry_steps": [
        {
            "key": "entry_steps",
            "label": "导航步骤 (JSON 数组)",
            "type": "json",
            "hint": "action: navigate/click_text/click_selector/wait/wait_network",
        },
    ],
}

# legacy schema aliases
NODE_FIELD_SCHEMAS["list_page"] = NODE_FIELD_SCHEMAS["extract_list"]
NODE_FIELD_SCHEMAS["pagination"] = NODE_FIELD_SCHEMAS["paginate"]
NODE_FIELD_SCHEMAS["detail"] = NODE_FIELD_SCHEMAS["extract_detail"]
NODE_FIELD_SCHEMAS["limits"] = NODE_FIELD_SCHEMAS["save"]
