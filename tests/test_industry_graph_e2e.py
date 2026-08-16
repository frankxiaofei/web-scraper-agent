"""供应链图谱 E2E/API 测试（P2-T-011）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.app import app


async def _get(path: str):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


def test_supply_chain_page_renders():
    r = asyncio.run(_get("/insights/supply-chain"))
    assert r.status_code == 200
    assert "industry-cy-graph" in r.text
    assert "cytoscape" in r.text.lower()


def test_supply_chain_graph_api_with_mock():
    mock_payload = {
        "elements": {
            "nodes": [{"data": {"id": "c1", "label": "A公司", "type": "Company"}}],
            "edges": [],
        },
        "source": "neo4j",
    }
    with patch("src.industry.graph.neo4j_sync.get_neo4j_graph_service") as mock_svc:
        mock_svc.return_value.query_subgraph.return_value = mock_payload
        r = asyncio.run(_get("/api/industry/graph?depth=2&industry_code=A01"))
    assert r.status_code == 200
    body = r.json()
    assert len(body["elements"]["nodes"]) == 1


def test_merge_companies_action_mock():
    async def _post():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/api/ontology/actions/merge-companies",
                json={
                    "source_ids": ["550e8400-e29b-41d4-a716-446655440000"],
                    "target_id": "550e8400-e29b-41d4-a716-446655440001",
                },
            )

    with patch("src.web.ontology_routes.get_mdm_repository") as mock_mdm:
        repo = mock_mdm.return_value
        repo.schema_ready.return_value = True
        repo.merge_companies.return_value = {
            "target_id": "550e8400-e29b-41d4-a716-446655440001",
            "merged_source_ids": ["550e8400-e29b-41d4-a716-446655440000"],
        }
        with patch("src.web.ontology_routes.get_neo4j_graph_service") as mock_graph:
            mock_graph.return_value.configured = False
            r = asyncio.run(_post())
    assert r.status_code == 200
    assert r.json()["ok"] is True
