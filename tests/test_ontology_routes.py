"""Ontology confirm-industry API 测试。"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.app import app


async def _post(path: str, body: dict):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, json=body)


def test_confirm_industry_without_mdm_503():
    with patch("src.web.ontology_routes.get_mdm_repository", return_value=None):
        r = asyncio.run(
            _post(
                "/api/ontology/actions/confirm-industry",
                {
                    "company_id": str(uuid.uuid4()),
                    "industry_code": "A01",
                    "taxonomy": "GB2017",
                },
            )
        )
    assert r.status_code == 503


def test_confirm_industry_success():
    mock_mdm = MagicMock()
    mock_mdm.schema_ready.return_value = True
    company_id = uuid.uuid4()
    with patch("src.web.ontology_routes.get_mdm_repository", return_value=mock_mdm):
        r = asyncio.run(
            _post(
                "/api/ontology/actions/confirm-industry",
                {
                    "company_id": str(company_id),
                    "industry_code": "A01",
                    "taxonomy": "GB2017",
                },
            )
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["method"] == "manual"
    mock_mdm.upsert_company_industry.assert_called_once()
