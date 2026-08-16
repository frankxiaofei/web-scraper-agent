"""IndustryInsightsService 单元测试。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.industry_insights_service import IndustryInsightsService
from src.web.data_service import NoticeDataService


@pytest.fixture
def industry_sample_jsonl(tmp_path: Path):
    fixtures = json.loads((ROOT / "tests" / "fixtures" / "industry_notices.json").read_text(encoding="utf-8"))
    now = datetime.now()
    for i, doc in enumerate(fixtures):
        doc["scraped_at"] = (now - timedelta(days=i + 1)).isoformat()
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in fixtures:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return jsonl_path


def test_summary_from_jsonl(industry_sample_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = industry_sample_jsonl.parent

        svc = IndustryInsightsService(NoticeDataService())
        summary = svc.summary(domain="数字农业", days=30)

    assert summary["total_notices"] >= 2
    assert summary["coverage_provinces"] >= 2
    assert "total_companies" in summary


def test_supply_chain_table(industry_sample_jsonl):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = industry_sample_jsonl.parent

        svc = IndustryInsightsService(NoticeDataService())
        result = svc.supply_chain_table("domain:数字农业", days=30, limit=10)

    assert result["edges"]
    assert result["edges"][0]["notice_count"] >= 1


def test_unknown_domain_raises():
    svc = IndustryInsightsService(NoticeDataService())
    with pytest.raises(ValueError, match="unknown domain"):
        svc.load_domain_docs("不存在的域", 30)
