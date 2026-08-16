"""行业洞察 API 集成测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.app import app


@pytest.fixture
def industry_jsonl(tmp_path: Path):
    fixtures = json.loads((ROOT / "tests" / "fixtures" / "industry_notices.json").read_text(encoding="utf-8"))
    now = datetime.now()
    for i, doc in enumerate(fixtures):
        doc["scraped_at"] = (now - timedelta(days=i + 1)).isoformat()
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in fixtures:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return jsonl_path


def _reset_services():
    import src.web.data_service as data_mod
    import src.web.industry_insights_service as industry_mod

    data_mod._service = None
    industry_mod._service = None
    industry_mod._docs_cache.clear()
    industry_mod._result_cache.clear()


async def _get(path: str):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


def test_industry_summary_api(industry_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = industry_jsonl.parent
        _reset_services()
        r = asyncio.run(_get("/api/industry/summary?domain=数字农业&days=30"))
    assert r.status_code == 200
    body = r.json()
    assert "total_notices" in body
    assert "coverage_provinces" in body


def test_industry_distribution_api(industry_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = industry_jsonl.parent
        _reset_services()
        r = asyncio.run(_get("/api/industry/distribution?domain=数字农业&days=30"))
    assert r.status_code == 200
    assert "regions" in r.json()


def test_industry_heatmap_api(industry_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = industry_jsonl.parent
        _reset_services()
        r = asyncio.run(_get("/api/industry/product_line%3A田间作业/heatmap?days=30"))
    assert r.status_code == 200
    body = r.json()
    assert body["industry_type"] == "product_line"


def test_industry_supply_chain_api(industry_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = industry_jsonl.parent
        _reset_services()
        r = asyncio.run(_get("/api/industry/domain%3A数字农业/supply-chain?days=30"))
    assert r.status_code == 200
    assert "edges" in r.json()


def test_industry_sync_status():
    r = asyncio.run(_get("/api/industry/sync/status"))
    assert r.status_code == 200
    assert r.json()["status"] in ("idle", "running")


def test_industry_heatmap_page():
    r = asyncio.run(_get("/insights/industry-heatmap"))
    assert r.status_code == 200
    assert "行业分布热力" in r.text


def test_industry_taxonomy_gb2017():
    r = asyncio.run(_get("/api/industry/taxonomy?taxonomy=GB2017"))
    assert r.status_code == 200
    body = r.json()
    assert body["taxonomy"] == "GB2017"
    assert "tree" in body
    assert len(body["tree"]) >= 1


def test_industry_heatmap_city_api(industry_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = industry_jsonl.parent
        _reset_services()
        r = asyncio.run(
            _get("/api/industry/domain%3A数字农业/heatmap?level=city&region=CN-44&days=30")
        )
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == "city"
    assert body["parent_region"] == "CN-44"


def test_insights_map_page():
    r = asyncio.run(_get("/insights/map"))
    assert r.status_code == 200
    assert "GIS" in r.text


def test_industry_detail_page():
    r = asyncio.run(_get("/insights/industry/A01"))
    assert r.status_code == 200
    assert "A01" in r.text or "农业" in r.text
