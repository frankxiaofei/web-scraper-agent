"""Ontology enrichment batch job (Phase 1)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.core.config import get_settings
from src.core.models import BidNotice
from src.core.timezone_utils import app_now
from src.industry.enrichment.award_parser import parse_award_relations
from src.industry.enrichment.entity_resolver import get_entity_resolver
from src.industry.mdm.repository import get_mdm_repository
from src.industry.region_normalizer import normalize_region_to_province

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentResult:
    processed: int = 0
    skipped: int = 0
    errors: int = 0
    payloads: list[dict[str, Any]] = field(default_factory=list)


def _classify_industry_codes(notice: BidNotice) -> list[dict[str, Any]]:
    """Phase 1 简化：复用 agri 标记作为 GB2017 A 门类近似。"""
    hits: list[dict[str, Any]] = []
    if notice.is_agri_related:
        hits.append(
            {
                "code": "A01",
                "taxonomy": "GB2017",
                "confidence": float(notice.agri_confidence or 0.6),
                "method": "agri_classifier",
            }
        )
    return hits


def run_enrichment_batch(notices: list[BidNotice]) -> EnrichmentResult:
    """对一批公告执行 MDM enrichment（需 INDUSTRY_ENRICHMENT_ENABLED + DATABASE_URL）。"""
    settings = get_settings()
    if not settings.industry_enrichment_enabled:
        return EnrichmentResult(skipped=len(notices))

    mdm = get_mdm_repository(settings)
    resolver = get_entity_resolver()
    if not mdm or not mdm.schema_ready() or not resolver.available:
        logger.debug("enrichment skipped: MDM not ready")
        return EnrichmentResult(skipped=len(notices))

    result = EnrichmentResult()
    for notice in notices:
        try:
            region_code = None
            if notice.region:
                norm = normalize_region_to_province(notice.region)
                if norm:
                    region_code = norm[0]

            notice_id = notice.content_hash or notice.url
            award = parse_award_relations(notice)
            purchaser_name = award.issuer_name or notice.purchaser or notice.tender_party
            supplier_name = award.winner_name or notice.tender_party

            purchaser_id = resolver.resolve_company(
                purchaser_name,
                source_notice_id=notice_id,
            )
            supplier_id = resolver.resolve_company(
                supplier_name,
                source_notice_id=notice_id,
            )

            industry_hits = _classify_industry_codes(notice)
            for hit in industry_hits:
                if purchaser_id:
                    mdm.upsert_company_industry(
                        purchaser_id,
                        hit["code"],
                        hit["taxonomy"],
                        confidence=hit["confidence"],
                        method=hit["method"],
                        source_notice_id=notice_id,
                    )

            contract_id = mdm.upsert_contract_from_notice(notice)

            if settings.neo4j_sync_enabled and contract_id and supplier_id:
                _sync_award_edges_to_neo4j(
                    contract_id=str(contract_id),
                    winner_company_id=str(supplier_id),
                    issuer_company_id=str(purchaser_id) if purchaser_id else None,
                    notice_url=notice.url,
                    purchaser_id=purchaser_id,
                    supplier_id=supplier_id,
                    industry_hits=industry_hits,
                )

            payload = {
                "version": "enrichment_v1",
                "enriched_at": app_now().isoformat(),
                "company_ids": [str(x) for x in [purchaser_id, supplier_id] if x],
                "purchaser_company_id": str(purchaser_id) if purchaser_id else None,
                "supplier_company_id": str(supplier_id) if supplier_id else None,
                "industry_codes": industry_hits,
                "region_code": region_code,
                "region_level": "province" if region_code else None,
                "contract_id": str(contract_id) if contract_id else None,
                "award_parsed": award.to_dict() if award.winner_name else None,
            }
            result.payloads.append({"notice_url": notice.url, "enrichment": payload})
            result.processed += 1
        except Exception as exc:
            logger.warning("enrichment failed for %s: %s", notice.url[:60], exc)
            result.errors += 1

    return result


def _sync_award_edges_to_neo4j(
    *,
    contract_id: str,
    winner_company_id: str,
    issuer_company_id: str | None,
    notice_url: str,
    purchaser_id: Any,
    supplier_id: Any,
    industry_hits: list[dict[str, Any]],
) -> None:
    """Phase 2 P2-T-005: 写入 AWARDED_TO / ISSUED_BY 边。"""
    try:
        from src.industry.graph.neo4j_sync import get_neo4j_graph_service
        from src.industry.mdm.repository import get_mdm_repository

        graph = get_neo4j_graph_service()
        if not graph.configured:
            return
        mdm = get_mdm_repository()
        if mdm:
            for cid in {purchaser_id, supplier_id}:
                if not cid:
                    continue
                row = mdm.get_company(str(cid))
                if row:
                    graph.merge_company(
                        company_id=str(cid),
                        canonical_name=row["canonical_name"],
                        country_iso=row.get("country_iso") or "CN",
                    )
            for hit in industry_hits:
                graph.merge_industry(
                    code=hit["code"],
                    taxonomy=hit["taxonomy"],
                    name=hit.get("name") or hit["code"],
                    level=int(hit.get("level") or 1),
                )
                if supplier_id:
                    graph.merge_classified_as(
                        company_id=str(supplier_id),
                        industry_code=hit["code"],
                        taxonomy=hit["taxonomy"],
                    )
        graph.merge_awarded_to(
            contract_id=contract_id,
            winner_company_id=winner_company_id,
            issuer_company_id=issuer_company_id,
            notice_url=notice_url,
        )
    except Exception as exc:
        logger.warning("neo4j award sync failed: %s", exc)
