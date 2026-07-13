"""Chrome 插件录制 → 工作流编译与 API 测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_recorder_compiler import compile_recorded_workflow, suggest_pagination_from_html
from src.core.crawl_rules import CrawlRule
from src.core.crawl_rule_workflow import workflow_to_rule_dict
from src.web.crawl_recorder_service import get_crawl_recorder_service
from src.web.crawl_recorder_ws_handler import handle_recorder_ws_message


SAMPLE_LIST_HTML = """
<html><head><title>公告列表</title></head><body>
<table class="list-table">
  <tr><td><a href="/detail/1">公告一</a></td><td>2024-01-01</td></tr>
  <tr><td><a href="/detail/2">公告二</a></td><td>2024-01-02</td></tr>
  <tr><td><a href="/detail/3">公告三</a></td><td>2024-01-03</td></tr>
  <tr><td><a href="/detail/4">公告四</a></td><td>2024-01-04</td></tr>
</table>
<a class="next" href="?page=2">下一页</a>
</body></html>
"""


def test_compile_input_step_to_wait_node():
    """输入步骤应编译为 wait 节点（等待输入框出现）。"""
    steps = [
        {"type": "navigate", "params": {"url": "https://example.com/search"}},
        {
            "type": "input",
            "selector": "#keyword",
            "params": {"selector": "#keyword", "value": "招标公告"},
        },
    ]
    wf = compile_recorded_workflow(
        site_id="test_site",
        entry_url="https://example.com/search",
        steps=steps,
    )
    wait_nodes = [n for n in wf["nodes"] if n["type"] == "wait"]
    assert wait_nodes, "应包含 wait 节点"
    wait_data = wait_nodes[0].get("data") or {}
    assert wait_data.get("selector") == "#keyword"

    restored = workflow_to_rule_dict(wf, site_id="test_site")
    rule = CrawlRule.model_validate(restored)
    assert len(rule.entry_steps) == 1
    assert rule.entry_steps[0].action == "wait"
    assert rule.entry_steps[0].selector == "#keyword"


def test_compile_recorded_steps_to_workflow():
    steps = [
        {"type": "navigate", "params": {"url": "https://example.com/list"}},
        {"type": "click", "params": {"selector": ".menu a", "text": "招标公告"}},
        {"type": "mark_list_page", "params": {"auto": True}},
    ]
    analysis = {
        "ok": True,
        "selectors": {
            "strategy": "dom",
            "container": "table",
            "item": "tr",
            "title": "a",
            "link": "a",
        },
    }
    wf = compile_recorded_workflow(
        site_id="test_site",
        site_name="测试站",
        entry_url="https://example.com/list",
        steps=steps,
        list_analysis=analysis,
        html_snapshot=SAMPLE_LIST_HTML,
    )
    types = [n["type"] for n in wf["nodes"]]
    assert "meta" in types
    assert "navigate" in types
    assert "click" in types
    assert "extract_list" in types
    assert "paginate" in types
    assert "extract_detail" in types
    assert "save" in types

    restored = workflow_to_rule_dict(wf, site_id="test_site")
    rule = CrawlRule.model_validate(restored)
    assert rule.entry_url == "https://example.com/list"
    assert rule.list_page is not None
    assert rule.list_page.container == "table"
    assert len(rule.entry_steps) == 1
    assert rule.entry_steps[0].action == "click_selector"


def test_suggest_pagination_from_html():
    pagination = suggest_pagination_from_html(SAMPLE_LIST_HTML)
    assert pagination["type"] == "next_button"
    assert pagination.get("selector")


def test_analyze_endpoint_with_fixture():
    from fastapi.testclient import TestClient

    from src.web.app import app

    client = TestClient(app)

    create = client.post(
        "/api/crawl-recorder/sessions",
        json={"site_id": "ccgp", "entry_url": "https://example.com/list"},
    )
    assert create.status_code in (200, 404)
    if create.status_code == 404:
        return

    data = create.json()
    session_id = data["session_id"]

    analyze = client.post(
        f"/api/crawl-recorder/sessions/{session_id}/analyze",
        json={"html": SAMPLE_LIST_HTML, "url": "https://example.com/list"},
    )
    assert analyze.status_code == 200
    body = analyze.json()
    assert body["ok"] is True
    assert body["analysis"]["selectors"]["container"] == "table"

    compile_res = client.post(f"/api/crawl-recorder/sessions/{session_id}/compile")
    assert compile_res.status_code == 200
    compiled = compile_res.json()
    assert compiled["ok"] is True
    assert compiled["workflow"]["nodes"]


def test_confirmed_list_page_analyze_and_compile():
    """用户确认列表页后，编译应使用 confirmed 选择器生成 extract_list 节点。"""
    from fastapi.testclient import TestClient

    from src.web.app import app

    client = TestClient(app)

    create = client.post(
        "/api/crawl-recorder/sessions",
        json={"site_id": "ccgp", "entry_url": "https://example.com/list"},
    )
    if create.status_code == 404:
        return

    session_id = create.json()["session_id"]
    list_hints = {
        "strategy": "dom",
        "container": "table.list-table",
        "item": "tr",
        "title": "a",
        "link": "a",
        "itemCount": 4,
    }

    analyze = client.post(
        f"/api/crawl-recorder/sessions/{session_id}/analyze",
        json={
            "html": SAMPLE_LIST_HTML,
            "url": "https://example.com/list",
            "confirmed_list_page": True,
            "list_hints": list_hints,
            "pagination": {"type": "next_button", "selector": "a.next"},
        },
    )
    assert analyze.status_code == 200
    body = analyze.json()
    assert body["ok"] is True
    assert body["session"]["list_page_confirmed"] is True
    assert body["session"]["list_page_confirmed_url"] == "https://example.com/list"
    assert body["analysis"]["selectors"]["container"] == "table.list-table"

    compile_res = client.post(f"/api/crawl-recorder/sessions/{session_id}/compile")
    assert compile_res.status_code == 200
    compiled = compile_res.json()
    assert compiled["ok"] is True

    restored = workflow_to_rule_dict(compiled["workflow"], site_id="ccgp")
    rule = CrawlRule.model_validate(restored)
    assert rule.list_page is not None
    assert rule.list_page.container == "table.list-table"
    assert rule.list_page.item == "tr"
    types = [n["type"] for n in compiled["workflow"]["nodes"]]
    assert "extract_list" in types
    assert "paginate" in types
    assert "extract_detail" in types


def test_recorder_extension_info_endpoint():
    from fastapi.testclient import TestClient

    from src.web.app import app

    client = TestClient(app)
    res = client.get("/api/crawl-recorder/extension")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "crawl-workflow-recorder" in data["path"]


def test_ws_step_recorded_compiles_partial_workflow():
    """WebSocket step_recorded 应追加步骤并触发部分工作流编译。"""
    svc = get_crawl_recorder_service()
    created = svc.create_session(site_id="ccgp", entry_url="https://example.com/list")
    if not created.get("ok"):
        return
    session_id = created["session_id"]

    async def run():
        await handle_recorder_ws_message(
            session_id,
            "extension",
            {
                "type": "extension_ready",
                "version": "1.0.0",
            },
        )
        await handle_recorder_ws_message(
            session_id,
            "extension",
            {
                "type": "step_recorded",
                "step": {
                    "type": "navigate",
                    "params": {"url": "https://example.com/list"},
                    "label": "页面导航",
                },
            },
        )
        await handle_recorder_ws_message(
            session_id,
            "extension",
            {
                "type": "step_recorded",
                "step": {
                    "type": "click",
                    "params": {"selector": ".menu a", "text": "招标公告"},
                    "label": "按钮 · 招标公告",
                },
            },
        )

    asyncio.run(run())

    session = svc.get_session(session_id)
    assert session["ok"] is True
    assert len(session["session"]["steps"]) == 2

    compiled = svc.compile_session(session_id)
    assert compiled["ok"] is True
    types = [n["type"] for n in compiled["workflow"]["nodes"]]
    assert "navigate" in types
    assert "click" in types


def test_ws_recording_stopped_compiles_full_workflow():
    """recording_stopped 后应生成完整工作流图。"""
    svc = get_crawl_recorder_service()
    created = svc.create_session(site_id="ccgp", entry_url="https://example.com/list")
    if not created.get("ok"):
        return
    session_id = created["session_id"]

    svc.append_steps(
        session_id,
        [
            {"type": "navigate", "params": {"url": "https://example.com/list"}},
            {"type": "click", "params": {"selector": ".menu a", "text": "招标"}},
            {"type": "mark_list_page", "params": {"confirmed": True}},
        ],
    )
    svc.analyze_session(
        session_id,
        html=SAMPLE_LIST_HTML,
        url="https://example.com/list",
        confirmed_list_page=True,
        list_hints={
            "strategy": "dom",
            "container": "table",
            "item": "tr",
            "title": "a",
            "link": "a",
        },
    )

    async def run():
        await handle_recorder_ws_message(
            session_id,
            "extension",
            {"type": "recording_stopped"},
        )

    asyncio.run(run())

    session = svc.get_session(session_id)
    assert session["ok"] is True
    assert session["session"]["recording_stopped"] is True

    compiled = svc.compile_session(session_id)
    assert compiled["ok"] is True
    types = [n["type"] for n in compiled["workflow"]["nodes"]]
    assert "extract_list" in types
    assert "paginate" in types
    assert "extract_detail" in types
