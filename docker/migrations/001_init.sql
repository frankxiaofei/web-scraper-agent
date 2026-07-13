-- Phase 2: bid_notices + scrape_jobs 表结构
-- 用法: psql $DATABASE_URL -f docker/migrations/001_init.sql

CREATE TABLE IF NOT EXISTS bid_notices (
    id              BIGSERIAL PRIMARY KEY,
    source          VARCHAR(64)  NOT NULL,
    source_id       VARCHAR(255),
    title           TEXT         NOT NULL,
    publish_date    TIMESTAMPTZ,
    detail_url      TEXT         NOT NULL,
    content_hash    VARCHAR(32)  NOT NULL,
    source_site_name TEXT,
    source_url      TEXT,
    region          TEXT,
    category        VARCHAR(32),
    purchaser       TEXT,
    agency          TEXT,
    crawled_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_bid_notices_content_hash UNIQUE (content_hash),
    CONSTRAINT uq_bid_notices_detail_url UNIQUE (detail_url)
);

CREATE INDEX IF NOT EXISTS idx_bid_notices_source ON bid_notices (source);
CREATE INDEX IF NOT EXISTS idx_bid_notices_publish_date ON bid_notices (publish_date DESC);
CREATE INDEX IF NOT EXISTS idx_bid_notices_crawled_at ON bid_notices (crawled_at DESC);

CREATE TABLE IF NOT EXISTS scrape_jobs (
    id                  BIGSERIAL PRIMARY KEY,
    job_id              VARCHAR(128),
    site_id             VARCHAR(64)  NOT NULL,
    status              VARCHAR(32)  NOT NULL,
    duration_seconds    DOUBLE PRECISION DEFAULT 0,
    error_message       TEXT,
    notices_count       INTEGER DEFAULT 0,
    new_notices_count   INTEGER DEFAULT 0,
    started_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_scrape_jobs_site_id ON scrape_jobs (site_id);
CREATE INDEX IF NOT EXISTS idx_scrape_jobs_started_at ON scrape_jobs (started_at DESC);
