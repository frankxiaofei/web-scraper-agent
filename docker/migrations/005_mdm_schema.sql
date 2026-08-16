-- Phase 1: MDM schema (PostGIS)
-- Usage: psql $DATABASE_URL -f docker/migrations/005_mdm_schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS mdm;

CREATE TABLE IF NOT EXISTS mdm.industries (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(16) NOT NULL,
    taxonomy        VARCHAR(16) NOT NULL,
    name            TEXT NOT NULL,
    name_en         TEXT,
    parent_code     VARCHAR(16),
    level           SMALLINT NOT NULL CHECK (level BETWEEN 1 AND 4),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_industries_code_taxonomy UNIQUE (code, taxonomy)
);

CREATE INDEX IF NOT EXISTS idx_industries_parent ON mdm.industries (parent_code, taxonomy);
CREATE INDEX IF NOT EXISTS idx_industries_name_trgm ON mdm.industries USING gin (name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS mdm.industry_crosswalk (
    id              SERIAL PRIMARY KEY,
    source_taxonomy VARCHAR(16) NOT NULL,
    source_code     VARCHAR(16) NOT NULL,
    target_taxonomy VARCHAR(16) NOT NULL,
    target_code     VARCHAR(16) NOT NULL,
    mapping_type    VARCHAR(16) NOT NULL DEFAULT 'approximate',
    confidence      NUMERIC(4,3) DEFAULT 1.0,
    version         VARCHAR(16) NOT NULL DEFAULT '2026.1',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_crosswalk UNIQUE (source_taxonomy, source_code, target_taxonomy, target_code, version)
);

CREATE TABLE IF NOT EXISTS mdm.companies (
    company_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    canonical_name  TEXT NOT NULL,
    display_name    TEXT,
    lei             VARCHAR(20),
    credit_code     VARCHAR(32),
    country_iso     CHAR(2) NOT NULL DEFAULT 'CN',
    aliases         JSONB NOT NULL DEFAULT '[]'::jsonb,
    quality_tier    CHAR(1) NOT NULL DEFAULT 'B',
    source_system   VARCHAR(64),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_companies_canonical_name ON mdm.companies (canonical_name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_canonical_country ON mdm.companies (canonical_name, country_iso);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_credit_code ON mdm.companies (credit_code) WHERE credit_code IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_lei ON mdm.companies (lei) WHERE lei IS NOT NULL;

CREATE TABLE IF NOT EXISTS mdm.regions (
    region_code     VARCHAR(32) PRIMARY KEY,
    name            TEXT NOT NULL,
    name_en         TEXT,
    level           VARCHAR(16) NOT NULL,
    country_iso     CHAR(2) NOT NULL DEFAULT 'CN',
    parent_code     VARCHAR(32) REFERENCES mdm.regions(region_code),
    adcode          VARCHAR(12),
    centroid        GEOGRAPHY(POINT, 4326),
    geom            GEOGRAPHY(MULTIPOLYGON, 4326),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_regions_parent ON mdm.regions (parent_code);
CREATE INDEX IF NOT EXISTS idx_regions_geom ON mdm.regions USING gist (geom);

CREATE TABLE IF NOT EXISTS mdm.contracts (
    contract_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    notice_mongo_id VARCHAR(32),
    notice_content_hash VARCHAR(32),
    title           TEXT,
    amount          NUMERIC(18,2),
    currency        CHAR(3) NOT NULL DEFAULT 'CNY',
    award_date      DATE,
    publish_date    DATE,
    notice_url      TEXT,
    source_site_id  VARCHAR(64),
    category        VARCHAR(32),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_contracts_notice_url UNIQUE (notice_url)
);

CREATE TABLE IF NOT EXISTS mdm.company_industries (
    id              BIGSERIAL PRIMARY KEY,
    company_id      UUID NOT NULL REFERENCES mdm.companies(company_id),
    industry_code   VARCHAR(16) NOT NULL,
    taxonomy        VARCHAR(16) NOT NULL,
    confidence      NUMERIC(4,3) NOT NULL DEFAULT 0.5,
    method          VARCHAR(32) NOT NULL DEFAULT 'keyword',
    source_notice_id VARCHAR(32),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_company_industry UNIQUE (company_id, industry_code, taxonomy)
);

CREATE TABLE IF NOT EXISTS mdm.lineage_events (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     VARCHAR(32) NOT NULL,
    entity_id       TEXT NOT NULL,
    event_type      VARCHAR(32) NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lineage_entity ON mdm.lineage_events (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_lineage_created ON mdm.lineage_events (created_at DESC);
