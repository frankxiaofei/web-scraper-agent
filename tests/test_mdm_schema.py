"""MDM schema & repository unit tests."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.industry.mdm.repository import MdmRepository, canonical_company_name


def test_canonical_company_name_strips_suffix():
    assert canonical_company_name("某某科技有限公司") == "某某科技"
    assert canonical_company_name("  ") == ""


@pytest.mark.skipif(
    not __import__("os").environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set",
)
def test_mdm_schema_and_company_upsert():
    import os

    repo = MdmRepository(os.environ["DATABASE_URL"])
    if not repo.schema_ready():
        from importlib.util import spec_from_file_location, module_from_spec

        mig = ROOT / "migrations" / "versions" / "20260816_0001_mdm_schema.py"
        spec = spec_from_file_location("mdm_mig", mig)
        mod = module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        mod.upgrade(os.environ["DATABASE_URL"])

    cid = repo.upsert_company("测试农业科技有限公司", source_system="pytest")
    assert isinstance(cid, UUID)
    cid2 = repo.upsert_company("测试农业科技有限公司")
    assert cid2 == cid
