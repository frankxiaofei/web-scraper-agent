"""商业供应链 API stub（Phase 3 P3-T-007，Panjiva 可选）。"""

from __future__ import annotations

from typing import Any

from src.core.config import get_settings


class CommercialSupplyChainError(Exception):
    pass


def fetch_commercial_supply_chain(
    company_name: str,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """预留 Panjiva/ImportGenius 等付费 API；未配置时返回 stub。"""
    settings = get_settings()
    api_key = getattr(settings, "panjiva_api_key", None)
    if not api_key:
        return {
            "source": "stub",
            "company": company_name,
            "edges": [],
            "message": "PANJIVA_API_KEY not configured; returning empty graph",
        }
    raise CommercialSupplyChainError("Panjiva integration not implemented")
