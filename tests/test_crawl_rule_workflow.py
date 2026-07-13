"""crawl rule workflow 转换单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_rule_workflow import (
    ACTION_NODE_TYPES,
    create_palette_node,
    parse_workflow_payload,
    rule_to_workflow,
    workflow_to_rule_dict,
    workflow_to_yaml,
    yaml_to_workflow,
)
from src.core.crawl_rules import CrawlRule, CrawlLimits, DetailRule, EntryStep, ListPage, Pagination


def _sample_rule() -> CrawlRule:
    return CrawlRule(
        site_id="test_site",
        name="测试站点",
        enabled=True,
        entry_url="https://example.com/list",
        entry_steps=[
            EntryStep(action="click_text", text="公告列表"),
            EntryStep(action="wait", selector=".list", timeout_ms=5000),
        ],
        list_page=ListPage(strategy="dom", container="#list", item="li", title="a", link="a"),
        pagination=Pagination(type="next_button", selector=".next", max_pages=10),
        detail=DetailRule(fetch_detail=True, content_selector="#content"),
        limits=CrawlLimits(max_pages=10, max_items=50, rate_limit_seconds=1.0),
    )


def test_rule_to_workflow_has_action_nodes():
    wf = rule_to_workflow(_sample_rule())
    types = [n["type"] for n in wf["nodes"]]
    assert "meta" in types
    assert "navigate" in types
    assert "click" in types
    assert "wait" in types
    assert "extract_list" in types
    assert "paginate" in types
    assert "extract_detail" in types
    assert "save" in types
    assert all(n.get("position") for n in wf["nodes"])
    assert wf["edges"][0]["from"] == "meta"


def test_yaml_to_workflow_alias():
    rule = _sample_rule()
    wf1 = rule_to_workflow(rule)
    wf2 = yaml_to_workflow(rule.model_dump(mode="json"))
    assert len(wf1["nodes"]) == len(wf2["nodes"])


def test_workflow_roundtrip():
    rule = _sample_rule()
    wf = rule_to_workflow(rule)
    restored = workflow_to_rule_dict(wf, site_id="test_site")
    roundtripped = CrawlRule.model_validate(restored)
    assert roundtripped.site_id == rule.site_id
    assert roundtripped.entry_url == rule.entry_url
    assert len(roundtripped.entry_steps) == 2
    assert roundtripped.entry_steps[0].action == "click_text"
    assert roundtripped.list_page is not None
    assert roundtripped.list_page.container == "#list"
    assert roundtripped.pagination is not None
    assert roundtripped.pagination.max_pages == 10
    assert roundtripped.detail is not None
    assert roundtripped.detail.fetch_detail is True
    assert roundtripped.limits.max_items == 50


def test_workflow_to_yaml_valid():
    wf = rule_to_workflow(_sample_rule())
    yaml_text = workflow_to_yaml(wf, site_id="test_site")
    parsed = yaml.safe_load(yaml_text)
    assert parsed["site_id"] == "test_site"
    assert parsed["list_page"]["container"] == "#list"
    assert len(parsed["entry_steps"]) == 2


def test_parse_workflow_payload_invalid_strategy():
    wf = rule_to_workflow(_sample_rule())
    for node in wf["nodes"]:
        if node["type"] == "extract_list":
            node["data"]["strategy"] = "invalid"
    rule, errors = parse_workflow_payload(wf, site_id="test_site")
    assert rule is None
    assert errors


def test_create_palette_node():
    node = create_palette_node("navigate")
    assert node["type"] == "navigate"
    assert node["id"].startswith("navigate_")
    assert "position" in node


def test_action_node_types_defined():
    assert "navigate" in ACTION_NODE_TYPES
    assert "extract_list" in ACTION_NODE_TYPES


def test_workflow_custom_edge_order():
    """edges 拓扑决定 workflow_to_rule_dict 中的节点执行顺序。"""
    wf = rule_to_workflow(_sample_rule())
    nodes = {n["type"]: n for n in wf["nodes"]}
    meta = nodes["meta"]
    navigate = nodes["navigate"]
    click = nodes["click"]
    wait = nodes["wait"]
    extract_list = nodes["extract_list"]
    paginate = nodes["paginate"]
    extract_detail = nodes["extract_detail"]
    save = nodes["save"]

    wf["edges"] = [
        {"from": meta["id"], "to": click["id"], "fromPort": "out", "toPort": "in"},
        {"from": click["id"], "to": navigate["id"], "fromPort": "out", "toPort": "in"},
        {"from": navigate["id"], "to": wait["id"], "fromPort": "out", "toPort": "in"},
        {"from": wait["id"], "to": extract_list["id"], "fromPort": "out", "toPort": "in"},
        {"from": extract_list["id"], "to": paginate["id"], "fromPort": "out", "toPort": "in"},
        {"from": paginate["id"], "to": extract_detail["id"], "fromPort": "out", "toPort": "in"},
        {"from": extract_detail["id"], "to": save["id"], "fromPort": "out", "toPort": "in"},
    ]

    restored = workflow_to_rule_dict(wf, site_id="test_site")
    assert restored["entry_url"] == "https://example.com/list"
    assert len(restored["entry_steps"]) == 2
    assert restored["entry_steps"][0]["action"] == "click_text"
    assert restored["entry_steps"][1]["action"] == "wait"

    yaml_text = workflow_to_yaml(wf, site_id="test_site")
    parsed = yaml.safe_load(yaml_text)
    assert parsed["entry_steps"][0]["text"] == "公告列表"
