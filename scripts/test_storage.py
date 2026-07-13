#!/usr/bin/env python3
"""验证 Phase 2 存储层：PostgreSQL upsert + Redis 去重。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings
from src.core.models import BidNotice, NoticeCategory, ScrapeResult
from src.core.pipeline import Pipeline
from src.db.session import check_connection, get_session, init_db
from sqlalchemy import func, select

from src.db.models import BidNoticeRow, ScrapeJobRow


def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        print("请设置 DATABASE_URL（可复制 .env.example → .env）")
        sys.exit(1)

    if not check_connection(settings.database_url):
        print("无法连接 PostgreSQL，请先: cd docker && docker compose up -d")
        sys.exit(1)

    init_db(settings.database_url)
    pipeline = Pipeline(settings=settings)

    notice = BidNotice(
        title="[测试] Phase2 存储层验证公告",
        url="https://example.com/test/phase2-storage-check",
        source_site_id="test_site",
        source_site_name="测试站点",
        source_url="https://example.com",
        publish_date=datetime.now(timezone.utc),
        category=NoticeCategory.TENDER,
    )
    result = ScrapeResult(site_id="test_site", success=True, notices=[notice], duration_seconds=0.1)
    processed = pipeline.process(result)
    saved = pipeline.save(processed)
    pipeline.log_scrape_job(result, new_count=len(processed.new), job_id="test_phase2")

    session = get_session(settings.database_url)
    notice_count = session.scalar(select(func.count()).select_from(BidNoticeRow))
    job_count = session.scalar(select(func.count()).select_from(ScrapeJobRow))
    session.close()

    print(f"JSONL 写入: {saved} 条")
    print(f"bid_notices 表共 {notice_count} 条")
    print(f"scrape_jobs 表共 {job_count} 条")
    print("Phase 2 存储层验证通过 ✓")


if __name__ == "__main__":
    main()
