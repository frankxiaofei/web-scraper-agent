-- Phase 4: 详情页字段
-- 用法: psql $DATABASE_URL -f docker/migrations/002_detail_fields.sql

ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS budget TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS content_text TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS content_html TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS attachments JSONB;
