"""世舶 API 企业 enrichment（Phase 1 P1-T-011）。"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from src.core.gov_bid_client import GovBidClient, GovBidConfigurationError, get_gov_bid_client
from src.industry.mdm.repository import MdmRepository, get_mdm_repository

logger = logging.getLogger(__name__)


class GovBidEnricher:
    """调用世舶 API 补全企业工商信息并写入 MDM。"""

    def __init__(
        self,
        client: GovBidClient | None = None,
        mdm: MdmRepository | None = None,
    ) -> None:
        self._client = client or get_gov_bid_client()
        self._mdm = mdm

    @property
    def available(self) -> bool:
        return self._client.configured and self._mdm is not None and self._mdm.schema_ready()

    async def fetch_company_profile(self, company_name: str) -> dict[str, Any]:
        if not self._client.configured:
            raise GovBidConfigurationError("未配置 GOV_BID_API_KEY")
        name = (company_name or "").strip()
        if not name:
            return {}
        result = await self._client.get_company_profile(name)
        data = result.get("data") if isinstance(result, dict) else result
        return data if isinstance(data, dict) else {"raw": result}

    async def enrich_company(
        self,
        company_name: str,
        *,
        company_id: UUID | None = None,
        source_notice_id: str | None = None,
    ) -> dict[str, Any]:
        """拉取世舶 profile 并 upsert MDM companies 元数据。"""
        profile = await self.fetch_company_profile(company_name)
        if not profile:
            return {"company_name": company_name, "profile": {}, "applied": False}

        if not self.available:
            return {"company_name": company_name, "profile": profile, "applied": False}

        resolved_id = company_id
        if resolved_id is None:
            resolved_id = self._mdm.upsert_company(  # type: ignore[union-attr]
                company_name,
                display_name=profile.get("companyName") or company_name,
                source_system="gov_bid_api",
                metadata={"gov_bid_profile": profile},
            )

        if resolved_id and source_notice_id:
            self._mdm.record_lineage(  # type: ignore[union-attr]
                "company",
                str(resolved_id),
                "gov_bid_enriched",
                {"notice_id": source_notice_id, "company_name": company_name},
            )

        return {
            "company_id": str(resolved_id) if resolved_id else None,
            "company_name": company_name,
            "profile": profile,
            "applied": bool(resolved_id),
            "credit_code": profile.get("creditCode") or profile.get("unifiedSocialCreditCode"),
        }


_enricher: GovBidEnricher | None = None


def get_gov_bid_enricher() -> GovBidEnricher:
    global _enricher
    if _enricher is None:
        _enricher = GovBidEnricher(mdm=get_mdm_repository())
    return _enricher
