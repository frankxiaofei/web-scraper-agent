"""数据统计服务与 API 单元测试。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.app import app
from src.web.data_service import NoticeDataService
from src.web.stats_service import StatsService, _fill_daily, _load_site_metadata, _with_pct


@pytest.fixture
def sample_notices(tmp_path: Path):
    """构造 JSONL 测试数据（站点 crec_bidding 为 sites.yaml enabled MVP 站点）。"""
    now = datetime.now()
    docs = [
        {
            "title": "招标公告 A",
            "url": "https://example.com/a",
            "source_site_id": "crec_bidding",
            "source_site_name": "鲁班商务网",
            "category": "招标公告",
            "scraped_at": (now - timedelta(days=2)).isoformat(),
            "content_text": "正文 A",
        },
        {
            "title": "中标公告 B",
            "url": "https://example.com/b",
            "source_site_id": "crec_bidding",
            "source_site_name": "鲁班商务网",
            "category": "中标公告",
            "scraped_at": (now - timedelta(days=1)).isoformat(),
            "content_text": "正文 B",
        },
        {
            "title": "其他站点",
            "url": "https://example.com/c",
            "source_site_id": "unknown_site_xyz",
            "source_site_name": "未启用站点",
            "category": "招标公告",
            "scraped_at": now.isoformat(),
            "content_text": "应被 MVP 过滤",
        },
    ]
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return jsonl_path, docs


def test_with_pct_calculates_percentage():
    items = [{"name": "A", "count": 3}, {"name": "B", "count": 1}]
    result = _with_pct(items, total=4)
    assert result[0]["pct"] == 75.0
    assert result[1]["pct"] == 25.0


def test_fill_daily_includes_zero_days():
    today = datetime.now().strftime("%Y-%m-%d")
    series = _fill_daily({today: 5}, days=3)
    assert len(series) == 3
    assert series[-1]["count"] == 5
    assert any(p["count"] == 0 for p in series)


def test_stats_overview_from_jsonl(sample_notices):
    jsonl_path, _ = sample_notices
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = jsonl_path.parent

        data_svc = NoticeDataService()
        stats_svc = StatsService(data_svc)
        overview = stats_svc.get_overview()

    assert overview["has_data"] is True
    assert overview["total"] == 2  # unknown_site_xyz 被 MVP 过滤
    assert overview["site_count"] == 1
    assert overview["category_count"] == 2
    assert len(overview["by_site"]) == 1
    assert overview["by_site"][0]["id"] == "crec_bidding"
    assert overview["by_site"][0]["name"] == "鲁班商务网"
    assert overview["by_site"][0]["company_name"] == "中国铁路工程集团有限公司"
    assert overview["by_site"][0]["pct"] == 100.0
    assert len(overview["by_category"]) == 2
    assert len(overview["by_time"]["daily"]) == 30


def test_stats_overview_empty_jsonl(tmp_path: Path):
    jsonl_path = tmp_path / "notices.jsonl"
    jsonl_path.write_text("", encoding="utf-8")
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = tmp_path

        overview = StatsService(NoticeDataService()).get_overview()

    assert overview["has_data"] is False
    assert overview["total"] == 0


def test_stats_page_route(sample_notices):
    jsonl_path, _ = sample_notices
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = jsonl_path.parent

        import src.web.data_service as data_mod
        import src.web.stats_service as stats_mod

        data_mod._service = None
        stats_mod._service = None

        client = TestClient(app)
        resp = client.get("/stats")
        assert resp.status_code == 200
        assert "数据统计" in resp.text
        assert "总条数" in resp.text
        assert "中国铁路工程集团有限公司" in resp.text
        assert "公司名称" in resp.text

        data_mod._service = None
        stats_mod._service = None


def test_stats_api_overview(sample_notices):
    jsonl_path, _ = sample_notices
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = jsonl_path.parent

        import src.web.data_service as data_mod
        import src.web.stats_service as stats_mod

        data_mod._service = None
        stats_mod._service = None

        client = TestClient(app)
        resp = client.get("/api/stats/overview")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_source"] == "jsonl"
        assert body["total"] == 2
        assert "by_site" in body
        assert "by_time" in body

        data_mod._service = None
        stats_mod._service = None


def test_load_site_metadata_maps_company_from_parent():
    _, companies = _load_site_metadata()
    assert companies["crec_bidding"] == "中国铁路工程集团有限公司"
    assert companies["中国铁道建筑集团有限公司_物资采购网"] == "中国铁道建筑集团有限公司"
    assert companies["中国交通建设集团有限公司_供应链管理信息系统"] == "中国交通建设集团有限公司"
    assert companies["中国电力建设集团有限公司_公共资源交易服务平台"] == "中国电力建设集团有限公司"
    assert companies["中国能源建设集团有限公司_电子采购平台"] == "中国能源建设集团有限公司"


def test_stats_api_overview_includes_company_name(sample_notices):
    jsonl_path, _ = sample_notices
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = jsonl_path.parent

        import src.web.data_service as data_mod
        import src.web.stats_service as stats_mod

        data_mod._service = None
        stats_mod._service = None

        client = TestClient(app)
        body = client.get("/api/stats/overview").json()
        assert body["by_site"][0]["company_name"] == "中国铁路工程集团有限公司"

        data_mod._service = None
        stats_mod._service = None


def test_stats_mongo_aggregation_uses_pipeline():
    mock_coll = MagicMock()
    mock_coll.count_documents.return_value = 10
    mock_coll.aggregate.side_effect = [
        [{"_id": "site_a", "count": 6, "name": "站点A"}],
        [{"_id": "招标公告", "count": 8}],
    ]
    mock_coll.find_one.return_value = {"scraped_at": datetime.now()}
    mock_coll.find.return_value = [
        {"scraped_at": datetime.now()},
        {"crawled_at": datetime.now() - timedelta(days=3)},
    ]

    data_svc = MagicMock()
    data_svc.data_source = "mongodb"
    data_svc._mongo_coll = mock_coll
    data_svc._enabled_site_ids.return_value = ["site_a"]
    data_svc._mvp_source_filter_mongo.return_value = {"source_site_id": {"$in": ["site_a"]}}

    overview = StatsService(data_svc).get_overview()
    assert overview["total"] == 10
    assert overview["has_data"] is True
    assert overview["by_site"][0]["name"] == "站点A"
    mock_coll.count_documents.assert_called_once()
