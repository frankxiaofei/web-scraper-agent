"""Industry graph API tests (P2-T-006)."""

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


def test_industry_graph_unconfigured():
    with patch("src.industry.graph.neo4j_sync.get_neo4j_graph_service") as mock_svc:
        mock_svc.return_value.query_subgraph.return_value = {
            "elements": {"nodes": [], "edges": []},
            "source": "unconfigured",
        }
        r = asyncio.run(_get("/api/industry/graph?depth=2"))
    assert r.status_code == 200
    body = r.json()
    assert "elements" in body
    assert body["source"] == "unconfigured"
