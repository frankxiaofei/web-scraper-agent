"""TED adapter 单元测试（P2-T-010）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.ted import TedAdapter, _parse_ted_notice


def test_parse_ted_notice():
    notice = _parse_ted_notice(
        {"ND": "123", "TI": "Smart farming tender", "PD": "2026-08-01", "CY": "DE"},
        {"id": "ted_eu", "name": "TED EU", "url": "https://ted.europa.eu"},
    )
    assert notice is not None
    assert notice.title == "Smart farming tender"
    assert "123" in notice.url


@pytest.mark.asyncio
async def test_ted_fetch_list_mock():
    adapter = TedAdapter({"id": "ted_eu", "name": "TED", "url": "https://ted.europa.eu"}, MagicMock())
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "results": [{"ND": "99", "TI": "Agri IoT", "PD": "2026-08-01"}]
    }
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("src.adapters.ted.httpx.AsyncClient", return_value=mock_client):
        notices = await adapter.fetch_list(max_items=5)
    assert len(notices) == 1
    assert notices[0].title == "Agri IoT"
