-- Phase 5: 关键项目信息字段
-- 用法: psql $DATABASE_URL -f docker/migrations/003_key_info.sql

ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS contract_amount TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS budget_amount TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS tender_party TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS project_period TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS key_summary TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS bid_deadline TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS open_time TEXT;
ALTER TABLE bid_notices ADD COLUMN IF NOT EXISTS contact TEXT;
