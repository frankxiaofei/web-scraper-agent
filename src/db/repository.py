"""数据库读写仓储。"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.models import BidNotice, ScrapeResult
from src.db.models import BidNoticeRow, ScrapeJobRow
from src.db.session import get_session

logger = logging.getLogger(__name__)


def _extract_source_id(url: str) -> Optional[str]:
    """从 URL 路径末段提取站点侧 ID（尽力而为）。"""
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    last = path.split("/")[-1]
    if last and re.match(r"^[\w.-]+$", last):
        return last[:255]
    return None


def _notice_to_row(notice: BidNotice) -> dict:
    return {
        "source": notice.source_site_id,
        "source_id": _extract_source_id(notice.url),
        "title": notice.title,
        "publish_date": notice.publish_date,
        "detail_url": notice.url,
        "content_hash": notice.content_hash,
        "source_site_name": notice.source_site_name,
        "source_url": notice.source_url,
        "region": notice.region,
        "category": notice.category.value if notice.category else None,
        "purchaser": notice.purchaser,
        "agency": notice.agency,
        "budget": notice.budget,
        "contract_amount": notice.contract_amount,
        "budget_amount": notice.budget_amount,
        "tender_party": notice.tender_party,
        "project_period": notice.project_period,
        "key_summary": notice.key_summary,
        "bid_deadline": notice.bid_deadline,
        "open_time": notice.open_time,
        "contact": notice.contact,
        "content_text": notice.content_text,
        "content_html": notice.content_html,
        "attachments": notice.attachments or None,
        "crawled_at": notice.scraped_at,
        "updated_at": datetime.now(timezone.utc),
    }


class NoticeRepository:
    """bid_notices 批量 upsert。"""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def upsert_many(self, notices: list[BidNotice]) -> int:
        if not notices:
            return 0
        session = get_session(self._database_url)
        try:
            rows = [_notice_to_row(n) for n in notices]
            stmt = pg_insert(BidNoticeRow).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["content_hash"],
                set_={
                    "title": stmt.excluded.title,
                    "publish_date": stmt.excluded.publish_date,
                    "detail_url": stmt.excluded.detail_url,
                    "source_site_name": stmt.excluded.source_site_name,
                    "region": stmt.excluded.region,
                    "category": stmt.excluded.category,
                    "purchaser": stmt.excluded.purchaser,
                    "agency": stmt.excluded.agency,
                    "budget": stmt.excluded.budget,
                    "contract_amount": stmt.excluded.contract_amount,
                    "budget_amount": stmt.excluded.budget_amount,
                    "tender_party": stmt.excluded.tender_party,
                    "project_period": stmt.excluded.project_period,
                    "key_summary": stmt.excluded.key_summary,
                    "bid_deadline": stmt.excluded.bid_deadline,
                    "open_time": stmt.excluded.open_time,
                    "contact": stmt.excluded.contact,
                    "content_text": stmt.excluded.content_text,
                    "content_html": stmt.excluded.content_html,
                    "attachments": stmt.excluded.attachments,
                    "crawled_at": stmt.excluded.crawled_at,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            result = session.execute(stmt)
            session.commit()
            count = result.rowcount if result.rowcount >= 0 else len(notices)
            logger.info("PostgreSQL upsert %d 条公告", count)
            return count
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()


class ScrapeJobRepository:
    """scrape_jobs 任务日志。"""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def log_job(
        self,
        *,
        site_id: str,
        result: ScrapeResult,
        new_count: int,
        job_id: Optional[str] = None,
        started_at: Optional[datetime] = None,
    ) -> None:
        session = get_session(self._database_url)
        try:
            row = ScrapeJobRow(
                job_id=job_id,
                site_id=site_id,
                status="success" if result.success else "failure",
                duration_seconds=result.duration_seconds,
                error_message=result.error,
                notices_count=len(result.notices),
                new_notices_count=new_count,
                started_at=started_at or datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.commit()
            logger.debug("已记录 scrape_job: site=%s status=%s", site_id, row.status)
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def get_recent_jobs(self, limit: int = 10) -> list[dict]:
        """查询最近 scrape_jobs 记录（供健康检查 / 告警）。"""
        session = get_session(self._database_url)
        try:
            stmt = (
                select(ScrapeJobRow)
                .order_by(ScrapeJobRow.started_at.desc())
                .limit(limit)
            )
            rows = session.scalars(stmt).all()
            return [
                {
                    "site_id": r.site_id,
                    "status": r.status,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "error_message": r.error_message,
                    "notices_count": r.notices_count,
                    "new_notices_count": r.new_notices_count,
                }
                for r in rows
            ]
        finally:
            session.close()
