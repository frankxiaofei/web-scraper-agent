"""世舶（gov-bid.com）招投标 REST API 客户端。"""

from __future__ import annotations

import os
from typing import Any, Mapping

import httpx

from src.core.http_api_client import create_http_client

DEFAULT_BASE_URL = "https://gate.gov-bid.com/outer-gateway"


class GovBidConfigurationError(RuntimeError):
    """客户端缺少必要配置。"""


class GovBidAPIError(RuntimeError):
    """上游接口调用失败。"""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class GovBidClient:
    """封装世舶 REST API，并确保 API Key 只由服务端注入。"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = (api_key or os.getenv("GOV_BID_API_KEY") or "").strip()
        self.base_url = (base_url or os.getenv("GOV_BID_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise GovBidConfigurationError("未配置 GOV_BID_API_KEY")

        # 不接受调用方传入的 key，防止本地代理接口被用来转发任意凭证。
        body = {key: value for key, value in payload.items() if key != "key"}
        body["key"] = self.api_key
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            async with create_http_client() as client:
                response = await client.post(url, json=body, headers={"Accept": "application/json"})
            response.raise_for_status()
            result = response.json()
        except httpx.TimeoutException as exc:
            raise GovBidAPIError("世舶 API 请求超时", status_code=504) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise GovBidAPIError(f"世舶 API 返回 HTTP {status}", status_code=502) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise GovBidAPIError(f"世舶 API 响应异常: {exc}", status_code=502) from exc

        if not isinstance(result, dict):
            raise GovBidAPIError("世舶 API 返回了非对象 JSON")
        return result

    async def search_projects(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._post("bid/searchProjectApi", payload)

    async def get_structure(self, project_id: int, publish_time: str) -> dict[str, Any]:
        return await self._post(
            "bid/getZTBStructreDetail", {"id": project_id, "publishTime": publish_time}
        )

    async def get_project_detail(self, project_id: int, publish_time: str) -> dict[str, Any]:
        return await self._post(
            "bid/getZTBProjectDetail", {"id": project_id, "publishTime": publish_time}
        )

    async def get_project_files(self, project_id: int, publish_time: str) -> dict[str, Any]:
        return await self._post(
            "bid/getZTBProjectFiles", {"projectId": project_id, "publishTime": publish_time}
        )

    async def get_company_profile(self, company_name: str) -> dict[str, Any]:
        return await self._post("bid/companyProfileSummary", {"companyName": company_name})


def get_gov_bid_client() -> GovBidClient:
    return GovBidClient()
