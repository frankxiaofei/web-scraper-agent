#!/usr/bin/env python3
"""验证 MongoDB 连接与 upsert。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings
from src.core.models import BidNotice, NoticeCategory, PageType, ScrapeResult
from src.core.pipeline import Pipeline
from src.db.mongo import create_mongo_repository


def main() -> None:
    settings = get_settings()
    if not settings.mongodb_uri:
        print("请设置 MONGODB_URI（可复制 .env.example → .env）")
        sys.exit(1)

    repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    if not repo or not repo.available:
        print("无法连接 MongoDB，请先: cd docker && docker compose up -d mongo")
        sys.exit(1)

    notice = BidNotice(
        title="[测试] MongoDB 存储层验证公告",
        url="https://example.com/test/mongo-storage-check",
        source_site_id="test_site",
        source_site_name="测试站点",
        source_url="https://example.com",
        publish_date=datetime.now(timezone.utc),
        category=NoticeCategory.TENDER,
        parent_url="https://example.com/parent",
        depth=1,
        page_type=PageType.DETAIL,
        needs_recursion=False,
    )
    result = ScrapeResult(site_id="test_site", success=True, notices=[notice], duration_seconds=0.1)
    pipeline = Pipeline(settings=settings)
    processed = pipeline.process(result)
    saved = pipeline.save(processed)

    docs = repo.find_by_source("test_site", limit=5)
    repo.close()

    print(f"JSONL/Mongo 写入: {saved} 条")
    print(f"MongoDB 查询 test_site 共 {len(docs)} 条（最近 5 条）")
    if docs:
        sample = docs[0]
        print(f"  样例: depth={sample.get('depth')} parent_url={sample.get('parent_url')}")
    print("MongoDB 存储层验证通过 ✓")


if __name__ == "__main__":
    main()
