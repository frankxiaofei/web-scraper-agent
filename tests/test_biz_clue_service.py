"""商机洞察 Agent 服务单元测试。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.app import app as main_app
from src.web.biz_clue_service import (
    BizClueService,
    compute_opportunity_score,
    deduplicate_leads,
    identify_project_stage,
    match_product_lines,
)
from src.web.data_service import NoticeDataService


@pytest.fixture
def sample_notices(tmp_path: Path):
    now = datetime.now()
    docs = [
        {
            "title": "某市智慧农业物联网平台建设项目招标公告",
            "url": "https://example.com/agri1",
            "source_site_id": "crec_bidding",
            "source_site_name": "鲁班商务网",
            "category": "招标公告",
            "scraped_at": (now - timedelta(days=1)).isoformat(),
            "content_text": (
                "采购人：某市农业农村局\n预算金额：380万元\n"
                "要求：农业物联网、传感器部署、数字农业平台"
            ),
            "budget_amount": "380万元",
            "tender_party": "某市农业农村局",
            "region": "浙江省杭州市",
        },
        {
            "title": "普通钢材采购",
            "url": "https://example.com/steel",
            "source_site_id": "crec_bidding",
            "category": "招标公告",
            "scraped_at": now.isoformat(),
            "content_text": "钢材采购，办公用品",
        },
        {
            "title": "数字农业大数据平台采购意向公示",
            "url": "https://www.ccgp.gov.cn/example/agri2",
            "source_site_id": "中国电力建设集团有限公司_公共资源交易服务平台",
            "source_site_name": "电建平台",
            "category": "采购意向",
            "scraped_at": now.isoformat(),
            "content_text": "数字农业、农业大数据、采购意向，预算1200万元",
            "region": "江苏省南京市",
            "tender_party": "南京市农业农村局",
            "budget_amount": "1200万元",
        },
        {
            "title": "田间助手巡田系统与农事记录平台",
            "url": "https://example.com/field",
            "source_site_id": "crec_bidding",
            "category": "招标公告",
            "scraped_at": now.isoformat(),
            "content_text": "巡田、农事记录、地块管理、轨迹采集",
            "region": "山东省济南市",
        },
    ]
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return jsonl_path


def test_match_product_lines_platform():
    doc = {"title": "数字农业大数据平台建设", "content_text": "农业物联网、质量追溯"}
    matches = match_product_lines(doc)
    lines = {m["product_line"] for m in matches}
    assert "农业平台" in lines


def test_match_product_lines_field():
    doc = {"title": "田间作业系统", "content_text": "巡田、农事记录、轨迹采集"}
    matches = match_product_lines(doc)
    lines = {m["product_line"] for m in matches}
    assert "田间作业" in lines


def test_match_product_lines_ai():
    doc = {"title": "农业AI", "content_text": "农业大模型、病虫害识别、智能问答"}
    matches = match_product_lines(doc)
    lines = {m["product_line"] for m in matches}
    assert "AI能力" in lines


def test_identify_project_stage_procurement_intent():
    doc = {"title": "采购意向公示", "content_text": "数字农业平台采购意向"}
    stage = identify_project_stage(doc)
    assert stage["stage"] == "采购前置"


def test_identify_project_stage_formal_bid():
    doc = {"title": "公开招标公告", "content_text": "竞争性磋商采购", "category": "招标公告"}
    stage = identify_project_stage(doc)
    assert stage["stage"] == "正式采购"


def test_compute_opportunity_score_high_value():
    doc = {
        "title": "数字农业平台采购意向",
        "url": "https://www.ccgp.gov.cn/test",
        "content_text": "采购意向，预算500万元，数字农业、农业大数据",
        "category": "采购意向",
        "budget_amount": "500万元",
        "tender_party": "某市农业农村局",
        "region": "浙江省",
        "scraped_at": datetime.now().isoformat(),
    }
    matches = match_product_lines(doc)
    stage = identify_project_stage(doc)
    result = compute_opportunity_score(doc, product_matches=matches, stage_info=stage, source_level="P0")
    assert result["opportunity_score"] >= 50
    assert result["opportunity_level"] in ("S", "A", "B", "C")
    assert result["score_reasons"]


def test_deduplicate_leads_keeps_higher_score():
    leads = [
        {"dedup_key": "same-url", "opportunity_score": 60, "lead_id": "a"},
        {"dedup_key": "same-url", "opportunity_score": 80, "lead_id": "b"},
    ]
    result = deduplicate_leads(leads)
    assert len(result) == 1
    assert result[0]["opportunity_score"] == 80


def test_analyze_collected_data(sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = sample_notices.parent

        import src.web.agri_insights_service as agri_mod
        import src.web.biz_clue_service as biz_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None
        biz_mod._service = None
        biz_mod._analysis_cache.clear()

        svc = BizClueService()
        result = svc.analyze_collected_data(days=7, refresh=True)

    assert result["summary"]["new_count"] >= 2
    assert len(result["leads"]) >= 2
    lead = result["leads"][0]
    assert "opportunity_score" in lead
    assert "product_matches" in lead
    assert "project_stage" in lead


def test_generate_daily_brief(sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = sample_notices.parent

        import src.web.agri_insights_service as agri_mod
        import src.web.biz_clue_service as biz_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None
        biz_mod._service = None
        biz_mod._analysis_cache.clear()

        svc = BizClueService()
        brief = svc.generate_daily_brief(days=7)

    assert "数字农业线索日报" in brief["markdown"]
    assert "## 今日概览" in brief["markdown"]
    assert "## 优先跟进" in brief["markdown"]


def test_biz_clue_api_summary(sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = sample_notices.parent

        import src.web.agri_insights_service as agri_mod
        import src.web.biz_clue_service as biz_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None
        biz_mod._service = None
        biz_mod._analysis_cache.clear()
        agri_mod._docs_cache.clear()
        agri_mod._summary_cache.clear()

        client = TestClient(main_app)
        resp = client.get("/api/biz-clue/summary?days=7")
        assert resp.status_code == 200
        body = resp.json()
        assert "summary" in body
        assert body["summary"]["new_count"] >= 2


def test_biz_clue_api_leads_filter(sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = sample_notices.parent

        import src.web.agri_insights_service as agri_mod
        import src.web.biz_clue_service as biz_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None
        biz_mod._service = None
        biz_mod._analysis_cache.clear()
        agri_mod._docs_cache.clear()
        agri_mod._summary_cache.clear()

        client = TestClient(main_app)
        resp = client.get("/api/biz-clue/leads?days=7&product_line=田间作业")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        for item in body["items"]:
            assert any(pm["product_line"] == "田间作业" for pm in item["product_matches"])


def test_biz_clue_api_config():
    client = TestClient(main_app)
    resp = client.get("/api/biz-clue/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["config"]["version"]


def test_biz_clue_api_config_stages():
    client = TestClient(main_app)
    resp = client.get("/api/biz-clue/config/stages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["stages"]) >= 6


def test_biz_clue_config_page():
    client = TestClient(main_app)
    resp = client.get("/insights/config", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers.get("location") == "/insights/sop"

    readonly = client.get("/insights/config/readonly")
    assert readonly.status_code == 200
    assert "商机配置管理" in readonly.text
    assert "阶段词库" in readonly.text


def test_biz_clue_page(sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = sample_notices.parent
        settings.openai_api_key = None

        import src.web.agri_insights_service as agri_mod
        import src.web.biz_clue_service as biz_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None
        biz_mod._service = None
        biz_mod._analysis_cache.clear()
        agri_mod._docs_cache.clear()
        agri_mod._summary_cache.clear()

        client = TestClient(main_app)
        resp = client.get("/insights")
        assert resp.status_code == 200
        assert "商机洞察" in resp.text
        assert "智能商机分析 Agent" in resp.text
        assert "LLM 增强分析" not in resp.text
        assert "优先跟进" in resp.text
        assert "bizclue 配置" in resp.text
        assert "对话查询" not in resp.text
        assert "重新分析" not in resp.text
        assert "/insights/hermes" not in resp.text
