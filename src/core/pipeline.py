"""数据清洗、去重与入库 Pipeline。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.config import Settings, get_settings
from src.core.dedup import MemoryDedupStore, create_dedup_store
from src.core.models import BidNotice, ScrapeResult
from src.core.notice_tags import apply_notice_tags
from src.db.mongo import create_mongo_repository
from src.db.repository import NoticeRepository, ScrapeJobRepository
from src.db.session import check_connection, init_db

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


@dataclass
class ProcessResult:
    """去重处理结果：新公告 + 需补全正文的已有公告。"""

    new: list[BidNotice] = field(default_factory=list)
    enriched: list[BidNotice] = field(default_factory=list)

    @property
    def all_for_storage(self) -> list[BidNotice]:
        return self.new + self.enriched


class Pipeline:
    """抓取结果处理流水线：内存/Redis/JSONL 去重，PostgreSQL 可选 upsert。"""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        settings: Optional[Settings] = None,
        db_url: Optional[str] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._db_url = db_url or self._settings.database_url
        self._data_dir = Path(data_dir) if data_dir else self._settings.data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = self._data_dir / "notices.jsonl"

        self._memory = MemoryDedupStore()
        self._load_seen_from_disk()

        self._dedup = create_dedup_store(
            redis_url=self._settings.redis_url,
            ttl_seconds=self._settings.redis_dedup_ttl_seconds,
            memory=self._memory,
        )

        self._notice_repo: Optional[NoticeRepository] = None
        self._job_repo: Optional[ScrapeJobRepository] = None
        self._mongo_repo = create_mongo_repository(
            uri=self._settings.mongodb_uri,
            db_name=self._settings.mongodb_db,
            collection_name=self._settings.mongodb_collection,
        )
        if self._db_url and check_connection(self._db_url):
            try:
                init_db(self._db_url)
                self._notice_repo = NoticeRepository(self._db_url)
                self._job_repo = ScrapeJobRepository(self._db_url)
            except Exception as e:
                logger.warning("PostgreSQL 初始化失败，已降级为 JSONL: %s", e)

    def _content_hash(self, notice: BidNotice) -> str:
        date_part = ""
        if notice.publish_date:
            date_part = notice.publish_date.strftime("%Y-%m-%d")
        raw = f"{notice.url}|{notice.title}|{date_part}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _dedup_key(self, notice: BidNotice) -> str:
        return notice.content_hash or self._content_hash(notice)

    def _load_seen_from_disk(self) -> None:
        """从已有 JSONL 加载去重键，跨运行持久化。"""
        if not self._jsonl_path.exists():
            return
        try:
            with open(self._jsonl_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    url = record.get("url", "")
                    key = record.get("content_hash") or self._content_hash(
                        BidNotice.model_validate(record)
                    )
                    if url:
                        self._memory.mark_seen(url, key)
                    elif key:
                        self._memory.seen_hashes.add(key)
            logger.info("已从 %s 加载 %d 条历史记录", self._jsonl_path, len(self._memory.seen_hashes))
        except Exception as e:
            logger.warning("加载历史数据失败: %s", e)

    def _is_duplicate(self, notice: BidNotice) -> bool:
        key = self._dedup_key(notice)
        return self._dedup.is_duplicate(notice.url, key)

    def _mark_seen(self, notice: BidNotice) -> None:
        self._dedup.mark_seen(notice.url, self._dedup_key(notice))

    def clean(self, notice: BidNotice) -> BidNotice:
        """基础清洗：去空白、生成 content_hash、补全来源标签。"""
        notice.title = notice.title.strip()
        notice.url = notice.url.strip()
        notice.content_hash = self._content_hash(notice)
        return apply_notice_tags(notice)

    @staticmethod
    def _has_body_content(notice: BidNotice) -> bool:
        return bool((notice.content_text or "").strip() or (notice.content_html or "").strip())

    def process(self, result: ScrapeResult) -> ProcessResult:
        """处理单次抓取结果：新公告写入 JSONL；已有记录若含正文则 upsert 补全。"""
        if not result.success:
            logger.error("跳过失败任务 %s: %s", result.site_id, result.error)
            return ProcessResult()

        new_notices: list[BidNotice] = []
        enriched: list[BidNotice] = []
        for raw in result.notices:
            notice = self.clean(raw)
            if self._is_duplicate(notice):
                if self._has_body_content(notice):
                    enriched.append(notice)
                continue
            self._mark_seen(notice)
            new_notices.append(notice)

        all_for_classify = new_notices + enriched
        if all_for_classify:
            from src.core.agri_classifier import (
                agri_tags_for_tagged_documents,
                apply_agri_classification_to_notice,
            )

            for notice in all_for_classify:
                apply_agri_classification_to_notice(notice, rate_limit=True)
                self._sync_agri_notice_to_tagged_documents(notice, agri_tags_for_tagged_documents)

        logger.info(
            "Pipeline: 站点 %s 新增 %d / 正文补全 %d / 共 %d 条",
            result.site_id,
            len(new_notices),
            len(enriched),
            len(result.notices),
        )
        return ProcessResult(new=new_notices, enriched=enriched)

    def _save_jsonl(self, notices: list[BidNotice]) -> int:
        if not notices:
            return 0
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            for n in notices:
                record = n.model_dump(mode="json")
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("已写入 %d 条到 %s", len(notices), self._jsonl_path)
        return len(notices)

    def _save_postgres(self, notices: list[BidNotice]) -> int:
        """尝试 upsert 到 PostgreSQL，失败时静默降级。"""
        if not self._notice_repo or not notices:
            return 0
        try:
            return self._notice_repo.upsert_many(notices)
        except Exception as e:
            logger.warning("PostgreSQL 写入失败，已降级为 JSONL: %s", e)
            return 0

    def _sync_agri_notice_to_tagged_documents(
        self,
        notice: BidNotice,
        tag_builder,
    ) -> None:
        """农业相关公告同步写入 tagged_documents，便于 Hermes 按标签检索。"""
        if notice.is_agri_related is not True or not self._mongo_repo:
            return
        tags = tag_builder(notice)
        if not tags:
            return
        content = (notice.content_text or notice.key_summary or "")[:8000] or None
        try:
            self._mongo_repo.upsert_tagged_document(
                title=notice.title or notice.url,
                tags=tags,
                url=notice.url or None,
                content=content,
                site_id=notice.source_site_id or None,
                metadata={
                    "is_agri_related": notice.is_agri_related,
                    "agri_confidence": notice.agri_confidence,
                    "agri_reason": notice.agri_reason,
                    "notice_url": notice.url,
                },
                source="pipeline_agri_classify",
                content_hash=notice.content_hash or self._content_hash(notice),
            )
        except Exception as e:
            logger.debug("tagged_documents 农业同步跳过 %s: %s", notice.url[:60], e)

    def _save_mongo(self, notices: list[BidNotice]) -> int:
        """尝试 upsert 到 MongoDB，失败时静默降级。"""
        if not self._mongo_repo or not notices:
            return 0
        try:
            return self._mongo_repo.upsert_many(notices)
        except Exception as e:
            logger.warning("MongoDB 写入失败，已降级为 JSONL: %s", e)
            return 0

    def save(self, processed: ProcessResult | list[BidNotice]) -> int:
        """保存抓取结果：JSONL 仅写新公告；MongoDB/PostgreSQL upsert 含正文补全。"""
        if isinstance(processed, list):
            processed = ProcessResult(new=processed)
        count = self._save_jsonl(processed.new)
        to_upsert = processed.all_for_storage
        self._save_mongo(to_upsert)
        self._save_postgres(to_upsert)
        self._run_industry_enrichment(to_upsert)
        return count

    def _run_industry_enrichment(self, notices: list[BidNotice]) -> None:
        if not notices or not self._settings.industry_enrichment_enabled:
            return
        try:
            from src.industry.enrichment.job import run_enrichment_batch

            result = run_enrichment_batch(notices)
            if result.processed:
                logger.info(
                    "Industry enrichment: processed=%d skipped=%d errors=%d",
                    result.processed,
                    result.skipped,
                    result.errors,
                )
                self._apply_enrichment_payloads(result.payloads)
        except Exception as exc:
            logger.warning("Industry enrichment hook failed: %s", exc)

    def _apply_enrichment_payloads(self, payloads: list[dict]) -> None:
        if not self._mongo_repo or not payloads:
            return
        for item in payloads:
            url = item.get("notice_url")
            enrichment = item.get("enrichment")
            if not url or not enrichment:
                continue
            try:
                self._mongo_repo.set_industry_enrichment(url, enrichment)
            except Exception as exc:
                logger.debug("Mongo enrichment write skipped %s: %s", url[:60], exc)

    def log_scrape_job(
        self,
        result: ScrapeResult,
        new_count: int,
        job_id: Optional[str] = None,
        started_at: Optional[datetime] = None,
    ) -> None:
        """记录抓取任务日志（PostgreSQL 可用时）。"""
        if not self._job_repo:
            return
        try:
            self._job_repo.log_job(
                site_id=result.site_id,
                result=result,
                new_count=new_count,
                job_id=job_id,
                started_at=started_at,
            )
        except Exception as e:
            logger.warning("scrape_jobs 写入失败: %s", e)
