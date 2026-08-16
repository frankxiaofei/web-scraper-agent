"""MDM PostgreSQL 仓储 — companies / regions / industries / contracts。"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.core.config import Settings, get_settings
from src.core.models import BidNotice
from src.core.timezone_utils import app_now

logger = logging.getLogger(__name__)

_repo: Optional["MdmRepository"] = None

_COMPANY_SUFFIX_RE = re.compile(
    r"(有限责任公司|股份有限公司|有限公司|集团公司|集团|公司)$"
)


def canonical_company_name(name: str) -> str:
    """企业名规范化（去空白与常见后缀）。"""
    cleaned = (name or "").strip()
    if not cleaned:
        return ""
    cleaned = _COMPANY_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned or (name or "").strip()


class MdmRepository:
    """mdm schema CRUD（需 PostGIS PostgreSQL）。"""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        self._url = database_url
        self._engine = engine or create_engine(database_url)

    def schema_ready(self) -> bool:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'mdm' AND table_name = 'companies' LIMIT 1"
                    )
                ).first()
                return row is not None
        except Exception as exc:
            logger.debug("MDM schema check failed: %s", exc)
            return False

    def upsert_company(
        self,
        name: str,
        *,
        country_iso: str = "CN",
        display_name: str | None = None,
        source_system: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID | None:
        canonical = canonical_company_name(name)
        if not canonical:
            return None
        display = display_name or name.strip()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        sql = text(
            """
            INSERT INTO mdm.companies (canonical_name, display_name, country_iso, source_system, metadata)
            VALUES (:canonical, :display, :country, :source, CAST(:metadata AS jsonb))
            ON CONFLICT (canonical_name, country_iso) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                updated_at = now()
            RETURNING company_id
            """
        )
        with self._engine.begin() as conn:
            row = conn.execute(
                sql,
                {
                    "canonical": canonical,
                    "display": display,
                    "country": country_iso,
                    "source": source_system,
                    "metadata": meta_json,
                },
            ).first()
            return row[0] if row else None

    def record_lineage(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO mdm.lineage_events (entity_type, entity_id, event_type, metadata)
                    VALUES (:entity_type, :entity_id, :event_type, CAST(:metadata AS jsonb))
                    """
                ),
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "event_type": event_type,
                    "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                },
            )

    def upsert_company_industry(
        self,
        company_id: UUID,
        industry_code: str,
        taxonomy: str,
        *,
        confidence: float = 0.5,
        method: str = "keyword",
        source_notice_id: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO mdm.company_industries
                        (company_id, industry_code, taxonomy, confidence, method, source_notice_id)
                    VALUES (:company_id, :code, :taxonomy, :confidence, :method, :notice_id)
                    ON CONFLICT (company_id, industry_code, taxonomy) DO UPDATE SET
                        confidence = GREATEST(mdm.company_industries.confidence, EXCLUDED.confidence),
                        method = EXCLUDED.method
                    """
                ),
                {
                    "company_id": company_id,
                    "code": industry_code,
                    "taxonomy": taxonomy,
                    "confidence": confidence,
                    "method": method,
                    "notice_id": source_notice_id,
                },
            )

    def upsert_region(
        self,
        region_code: str,
        name: str,
        *,
        level: str = "province",
        country_iso: str = "CN",
        parent_code: str | None = None,
        adcode: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO mdm.regions (region_code, name, level, country_iso, parent_code, adcode)
                    VALUES (:code, :name, :level, :country, :parent, :adcode)
                    ON CONFLICT (region_code) DO UPDATE SET
                        name = EXCLUDED.name,
                        level = EXCLUDED.level,
                        parent_code = EXCLUDED.parent_code,
                        adcode = EXCLUDED.adcode
                    """
                ),
                {
                    "code": region_code,
                    "name": name,
                    "level": level,
                    "country": country_iso,
                    "parent": parent_code,
                    "adcode": adcode,
                },
            )

    def upsert_industry(
        self,
        code: str,
        taxonomy: str,
        name: str,
        *,
        level: int,
        parent_code: str | None = None,
        name_en: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO mdm.industries (code, taxonomy, name, name_en, parent_code, level)
                    VALUES (:code, :taxonomy, :name, :name_en, :parent, :level)
                    ON CONFLICT (code, taxonomy) DO UPDATE SET
                        name = EXCLUDED.name,
                        name_en = EXCLUDED.name_en,
                        parent_code = EXCLUDED.parent_code,
                        level = EXCLUDED.level,
                        updated_at = now()
                    """
                ),
                {
                    "code": code,
                    "taxonomy": taxonomy,
                    "name": name,
                    "name_en": name_en,
                    "parent": parent_code,
                    "level": level,
                },
            )

    def upsert_contract_from_notice(self, notice: BidNotice) -> UUID | None:
        if not notice.url:
            return None
        amount = None
        if notice.budget_amount:
            from src.core.notice_field_utils import parse_amount_yuan

            amount = parse_amount_yuan(notice.budget_amount)
        publish: date | None = None
        if notice.publish_date:
            publish = notice.publish_date.date() if isinstance(notice.publish_date, datetime) else notice.publish_date
        category = notice.category.value if notice.category else None
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO mdm.contracts
                        (notice_content_hash, title, amount, publish_date, notice_url, source_site_id, category)
                    VALUES (:hash, :title, :amount, :publish, :url, :site, :category)
                    ON CONFLICT (notice_url) DO UPDATE SET
                        title = EXCLUDED.title,
                        amount = EXCLUDED.amount,
                        publish_date = EXCLUDED.publish_date
                    RETURNING contract_id
                    """
                ),
                {
                    "hash": notice.content_hash,
                    "title": notice.title,
                    "amount": amount,
                    "publish": publish,
                    "url": notice.url,
                    "site": notice.source_site_id,
                    "category": category,
                },
            ).first()
            return row[0] if row else None

    def count_industries(self, taxonomy: str = "GB2017") -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT COUNT(*) FROM mdm.industries WHERE taxonomy = :taxonomy"),
                {"taxonomy": taxonomy},
            ).first()
            return int(row[0]) if row else 0

    def count_regions(self, level: str = "province") -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT COUNT(*) FROM mdm.regions WHERE level = :level"),
                {"level": level},
            ).first()
            return int(row[0]) if row else 0

    def get_company(self, company_id: str | UUID) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT company_id, canonical_name, display_name, country_iso "
                    "FROM mdm.companies WHERE company_id = :id"
                ),
                {"id": str(company_id)},
            ).mappings().first()
            return dict(row) if row else None

    def merge_companies(
        self,
        source_ids: list[UUID],
        target_id: UUID,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """合并多个企业到 target_id（P2-T-008）。"""
        merged: list[str] = []
        with self._engine.begin() as conn:
            target = conn.execute(
                text("SELECT company_id FROM mdm.companies WHERE company_id = :id"),
                {"id": str(target_id)},
            ).first()
            if not target:
                raise ValueError(f"target company not found: {target_id}")

            for sid in source_ids:
                if sid == target_id:
                    continue
                src = conn.execute(
                    text(
                        "SELECT company_id, canonical_name FROM mdm.companies WHERE company_id = :id"
                    ),
                    {"id": str(sid)},
                ).first()
                if not src:
                    continue
                conn.execute(
                    text(
                        """
                        UPDATE mdm.companies
                        SET aliases = aliases || CAST(:alias_json AS jsonb),
                            updated_at = now()
                        WHERE company_id = :target
                        """
                    ),
                    {
                        "target": str(target_id),
                        "alias_json": json.dumps(
                            [{"alias": src[1], "type": "merged", "source": reason or "merge_companies"}],
                            ensure_ascii=False,
                        ),
                    },
                )
                conn.execute(
                    text(
                        """
                        UPDATE mdm.company_industries SET company_id = :target
                        WHERE company_id = :source
                        """
                    ),
                    {"target": str(target_id), "source": str(sid)},
                )
                conn.execute(
                    text("DELETE FROM mdm.companies WHERE company_id = :source"),
                    {"source": str(sid)},
                )
                merged.append(str(sid))
        for sid in merged:
            self.record_lineage(
                "company",
                str(target_id),
                "merge_companies",
                {"source_id": sid, "reason": reason},
            )
        return {"target_id": str(target_id), "merged_source_ids": merged}


def get_mdm_repository(settings: Settings | None = None) -> MdmRepository | None:
    global _repo
    s = settings or get_settings()
    if not s.database_url:
        return None
    if _repo is None or _repo._url != s.database_url:
        _repo = MdmRepository(s.database_url)
    return _repo
