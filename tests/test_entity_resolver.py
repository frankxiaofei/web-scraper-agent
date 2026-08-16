"""EntityResolver unit tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.industry.enrichment.entity_resolver import EntityResolver
from src.industry.mdm.repository import canonical_company_name


def test_canonical_name_consistency():
    assert canonical_company_name("ABC有限公司") == "ABC"


def test_resolve_company_without_mdm():
    resolver = EntityResolver(None)
    assert resolver.resolve_company("某农业公司") is None


def test_resolve_company_with_mock_mdm():
    mdm = MagicMock()
    mdm.schema_ready.return_value = True
    expected = uuid4()
    mdm.upsert_company.return_value = expected
    resolver = EntityResolver(mdm)
    result = resolver.resolve_company("华南数字农业科技有限公司", source_notice_id="abc123")
    assert result == expected
    mdm.record_lineage.assert_called_once()
