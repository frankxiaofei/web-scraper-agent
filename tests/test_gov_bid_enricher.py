"""GovBidEnricher 单元测试。"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.gov_bid_client import GovBidConfigurationError
from src.industry.enrichment.gov_bid_enricher import GovBidEnricher


@pytest.mark.asyncio
async def test_enrich_company_without_api_key():
    client = MagicMock()
    client.configured = False
    enricher = GovBidEnricher(client=client, mdm=None)
    with pytest.raises(GovBidConfigurationError):
        await enricher.fetch_company_profile("测试公司")


@pytest.mark.asyncio
async def test_enrich_company_applies_to_mdm():
    client = MagicMock()
    client.configured = True
    client.get_company_profile = AsyncMock(
        return_value={"data": {"companyName": "测试公司", "creditCode": "91110000MA001"}}
    )
    mdm = MagicMock()
    mdm.schema_ready.return_value = True
    cid = uuid.uuid4()
    mdm.upsert_company.return_value = cid
    enricher = GovBidEnricher(client=client, mdm=mdm)
    result = await enricher.enrich_company("测试公司", source_notice_id="n1")
    assert result["applied"] is True
    assert result["credit_code"] == "91110000MA001"
    mdm.record_lineage.assert_called_once()
