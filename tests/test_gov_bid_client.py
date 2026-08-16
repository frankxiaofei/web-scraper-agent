from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.gov_bid_client import GovBidAPIError, GovBidClient, GovBidConfigurationError


def _mock_client(response: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_search_injects_server_key_and_keeps_search_fields():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"data": {"total": 1}}
    client = _mock_client(response)

    with patch("src.core.gov_bid_client.create_http_client", return_value=client):
        result = await GovBidClient(api_key="secret").search_projects(
            {"key": "untrusted", "pageID": 1, "keyword": "软件"}
        )

    assert result["data"]["total"] == 1
    call = client.post.call_args
    assert call.args[0].endswith("/bid/searchProjectApi")
    assert call.kwargs["json"] == {"pageID": 1, "keyword": "软件", "key": "secret"}


@pytest.mark.asyncio
async def test_files_uses_project_id_field_required_by_upstream():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"data": []}
    client = _mock_client(response)

    with patch("src.core.gov_bid_client.create_http_client", return_value=client):
        await GovBidClient(api_key="secret").get_project_files(123, "2026-06-08")

    assert client.post.call_args.kwargs["json"]["projectId"] == 123


@pytest.mark.asyncio
async def test_missing_key_fails_before_http_call():
    with pytest.raises(GovBidConfigurationError, match="GOV_BID_API_KEY"):
        await GovBidClient(api_key="").get_company_profile("示例公司")


@pytest.mark.asyncio
async def test_timeout_is_mapped_to_gateway_timeout():
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("src.core.gov_bid_client.create_http_client", return_value=client):
        with pytest.raises(GovBidAPIError) as exc_info:
            await GovBidClient(api_key="secret").get_company_profile("示例公司")

    assert exc_info.value.status_code == 504
