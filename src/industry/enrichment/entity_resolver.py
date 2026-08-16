"""企业实体解析 — 名称规范化 + MDM upsert。"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from src.industry.mdm.repository import MdmRepository, get_mdm_repository

logger = logging.getLogger(__name__)

_resolver: Optional["EntityResolver"] = None


class EntityResolver:
    """将公告中的企业名称解析为 MDM company_id。"""

    def __init__(self, mdm: MdmRepository | None = None) -> None:
        self._mdm = mdm

    @property
    def available(self) -> bool:
        return self._mdm is not None and self._mdm.schema_ready()

    def resolve_company(
        self,
        name: str | None,
        *,
        country_iso: str = "CN",
        source_notice_id: str | None = None,
        source_system: str = "pipeline_enrichment",
    ) -> UUID | None:
        if not name or not self._mdm:
            return None
        cleaned = name.strip()
        if len(cleaned) < 2:
            return None
        try:
            company_id = self._mdm.upsert_company(
                cleaned,
                country_iso=country_iso,
                display_name=cleaned,
                source_system=source_system,
                metadata={"source_notice_id": source_notice_id} if source_notice_id else None,
            )
            if company_id and source_notice_id:
                self._mdm.record_lineage(
                    "company",
                    str(company_id),
                    "resolved_from_notice",
                    {"notice_id": source_notice_id, "raw_name": cleaned},
                )
            return company_id
        except Exception as exc:
            logger.warning("entity resolve failed for %r: %s", cleaned[:40], exc)
            return None


def get_entity_resolver() -> EntityResolver:
    global _resolver
    if _resolver is None:
        _resolver = EntityResolver(get_mdm_repository())
    return _resolver
