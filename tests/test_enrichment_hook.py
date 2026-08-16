"""Pipeline enrichment hook tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.models import BidNotice
from src.core.pipeline import Pipeline


def _sample_notice(url: str) -> BidNotice:
    return BidNotice(
        title="t",
        url=url,
        source_site_id="test",
        source_site_name="测试站点",
        source_url="http://example.com/",
    )


def test_enrichment_skipped_when_disabled(tmp_path):
    with patch("src.core.pipeline.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.data_dir = tmp_path
        settings.mongodb_uri = None
        settings.database_url = None
        settings.redis_url = None
        settings.redis_dedup_ttl_seconds = 0
        settings.industry_enrichment_enabled = False
        pipe = Pipeline(settings=settings)
        notice = _sample_notice("http://example.com/n1")
        with patch("src.industry.enrichment.job.run_enrichment_batch") as mock_batch:
            pipe._run_industry_enrichment([notice])
            mock_batch.assert_not_called()


def test_enrichment_runs_when_enabled(tmp_path):
    with patch("src.core.pipeline.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.data_dir = tmp_path
        settings.mongodb_uri = None
        settings.database_url = None
        settings.redis_url = None
        settings.redis_dedup_ttl_seconds = 0
        settings.industry_enrichment_enabled = True
        pipe = Pipeline(settings=settings)
        notice = _sample_notice("http://example.com/n2")
        mock_result = MagicMock(processed=1, skipped=0, errors=0, payloads=[])
        with patch("src.industry.enrichment.job.run_enrichment_batch", return_value=mock_result) as mock_batch:
            pipe._run_industry_enrichment([notice])
            mock_batch.assert_called_once()
