"""Legacy insights API 兼容测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.app import app


async def _get(path: str):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


def test_agri_insights_still_works_with_deprecation():
    r = asyncio.run(_get("/api/agri/insights?days=7"))
    assert r.status_code == 200
    assert "Deprecation" in r.headers
    assert 'successor="/api/industry/"' in r.headers["Deprecation"]


def test_biz_clue_summary_deprecation_header():
    r = asyncio.run(_get("/api/biz-clue/summary?days=7"))
    assert r.status_code == 200
    assert "Deprecation" in r.headers
