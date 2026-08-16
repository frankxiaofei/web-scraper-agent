"""IndustryHeatmapService 单元测试。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.industry_heatmap_service import HeatmapLevel, HeatmapMetric, IndustryHeatmapService, IndustryInsightsService
from src.web.data_service import NoticeDataService


def _sample_jsonl(tmp_path: Path) -> Path:
    fixtures = json.loads((ROOT / "tests" / "fixtures" / "industry_notices.json").read_text(encoding="utf-8"))
    now = datetime.now()
    for i, doc in enumerate(fixtures):
        doc["scraped_at"] = (now - timedelta(days=i + 1)).isoformat()
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in fixtures:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return jsonl_path


def test_heatmap_product_line_filter(tmp_path):
    jsonl = _sample_jsonl(tmp_path)
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = jsonl.parent

        insights = IndustryInsightsService(NoticeDataService())
        svc = IndustryHeatmapService(insights)
        result = svc.heatmap("product_line:田间作业", metric=HeatmapMetric.COUNT, days=30)

    assert result["industry_type"] == "product_line"
    assert "regions" in result
    assert result["coverage_provinces"] >= 1


def test_heatmap_pct_calculation(tmp_path):
    jsonl = _sample_jsonl(tmp_path)
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = jsonl.parent

        insights = IndustryInsightsService(NoticeDataService())
        svc = IndustryHeatmapService(insights)
        result = svc.heatmap("domain:数字农业", metric=HeatmapMetric.COUNT, days=30)

    total_pct = sum(r["pct"] for r in result["regions"])
    assert 99 <= total_pct <= 101


def test_heatmap_city_level_drilldown(tmp_path):
    jsonl = _sample_jsonl(tmp_path)
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = jsonl.parent

        insights = IndustryInsightsService(NoticeDataService())
        svc = IndustryHeatmapService(insights)
        result = svc.heatmap(
            "domain:数字农业",
            metric=HeatmapMetric.COUNT,
            level=HeatmapLevel.CITY,
            days=30,
            region="CN-44",
        )

    assert result["level"] == "city"
    assert result["parent_region"] == "CN-44"
    assert result["coverage_cities"] >= 1
    city_names = {r["name"] for r in result["regions"]}
    assert "广州市" in city_names


def test_heatmap_city_requires_region(tmp_path):
    jsonl = _sample_jsonl(tmp_path)
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = jsonl.parent

        insights = IndustryInsightsService(NoticeDataService())
        svc = IndustryHeatmapService(insights)
        try:
            svc.heatmap("domain:数字农业", level=HeatmapLevel.CITY)
            raised = False
        except ValueError:
            raised = True
    assert raised
