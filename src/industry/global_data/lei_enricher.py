"""GLEIF / OpenCorporates LEI enrichment stub（Phase 3 P3-T-002）。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GLEIF_API = "https://api.gleif.org/api/v1/lei-records"


def lookup_lei(company_name: str, *, country: str = "CN") -> dict[str, Any] | None:
    """按企业名查询 LEI（免费 GLEIF API）。"""
    if not company_name.strip():
        return None
    try:
        resp = httpx.get(
            GLEIF_API,
            params={"filter[entity.legalName]": company_name, "page[size]": 1},
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if not data:
            return None
        attrs = data[0].get("attributes") or {}
        entity = attrs.get("entity") or {}
        return {
            "lei": data[0].get("id"),
            "legal_name": (entity.get("legalName") or {}).get("name"),
            "country": (entity.get("legalAddress") or {}).get("country"),
            "source": "gleif",
        }
    except Exception as exc:
        logger.debug("GLEIF lookup failed for %r: %s", company_name[:40], exc)
        return None
