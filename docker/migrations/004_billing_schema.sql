-- Commercial Phase C0: billing schema（用户、租户、用量计量）
-- 用法: psql $DATABASE_URL -f docker/migrations/004_billing_schema.sql

CREATE SCHEMA IF NOT EXISTS billing;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS billing.users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR(255) NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    display_name    VARCHAR(128),
    email_verified_at TIMESTAMPTZ,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_email UNIQUE (email)
);

CREATE INDEX IF NOT EXISTS idx_users_email ON billing.users (email);

CREATE TABLE IF NOT EXISTS billing.tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(256) NOT NULL,
    slug            VARCHAR(64) NOT NULL,
    plan_id         VARCHAR(32) NOT NULL DEFAULT 'free',
    stripe_customer_id VARCHAR(64),
    tax_id          VARCHAR(32),
    billing_email   VARCHAR(255),
    settings        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_tenants_slug UNIQUE (slug)
);

CREATE TABLE IF NOT EXISTS billing.tenant_members (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES billing.tenants(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES billing.users(id) ON DELETE CASCADE,
    role            VARCHAR(16) NOT NULL DEFAULT 'member'
                    CHECK (role IN ('admin', 'member', 'viewer')),
    invited_by      UUID REFERENCES billing.users(id),
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_tenant_member UNIQUE (tenant_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_members_user ON billing.tenant_members (user_id);

CREATE TABLE IF NOT EXISTS billing.subscriptions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES billing.tenants(id) ON DELETE CASCADE,
    plan_id                 VARCHAR(32) NOT NULL,
    status                  VARCHAR(24) NOT NULL DEFAULT 'active'
                            CHECK (status IN (
                                'active', 'trialing', 'past_due', 'canceled',
                                'incomplete', 'incomplete_expired', 'paused'
                            )),
    billing_cycle           VARCHAR(16) NOT NULL DEFAULT 'monthly'
                            CHECK (billing_cycle IN ('monthly', 'yearly')),
    payment_provider        VARCHAR(16) DEFAULT 'manual',
    stripe_subscription_id  VARCHAR(64),
    current_period_start    TIMESTAMPTZ NOT NULL,
    current_period_end      TIMESTAMPTZ NOT NULL,
    cancel_at_period_end    BOOLEAN NOT NULL DEFAULT FALSE,
    canceled_at             TIMESTAMPTZ,
    trial_end               TIMESTAMPTZ,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant ON billing.subscriptions (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_active_tenant
    ON billing.subscriptions (tenant_id)
    WHERE status IN ('active', 'trialing', 'past_due');

CREATE TABLE IF NOT EXISTS billing.usage_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES billing.tenants(id) ON DELETE CASCADE,
    user_id         UUID REFERENCES billing.users(id) ON DELETE SET NULL,
    metric          VARCHAR(64) NOT NULL,
    quantity        NUMERIC(18, 6) NOT NULL DEFAULT 1,
    unit            VARCHAR(16) NOT NULL DEFAULT 'count',
    period_key      VARCHAR(7) NOT NULL,
    idempotency_key VARCHAR(128),
    source          VARCHAR(64),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_usage_idempotency UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_metric_period
    ON billing.usage_events (tenant_id, metric, period_key);
CREATE INDEX IF NOT EXISTS idx_usage_events_recorded
    ON billing.usage_events (recorded_at DESC);

CREATE TABLE IF NOT EXISTS billing.usage_aggregates (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES billing.tenants(id) ON DELETE CASCADE,
    metric          VARCHAR(64) NOT NULL,
    period_key      VARCHAR(7) NOT NULL,
    total_quantity  NUMERIC(18, 6) NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_usage_aggregate UNIQUE (tenant_id, metric, period_key)
);

CREATE TABLE IF NOT EXISTS billing.invoices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES billing.tenants(id) ON DELETE CASCADE,
    subscription_id     UUID REFERENCES billing.subscriptions(id) ON DELETE SET NULL,
    number              VARCHAR(32) NOT NULL,
    status              VARCHAR(16) NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'open', 'paid', 'void', 'uncollectible')),
    amount_cny          INTEGER NOT NULL,
    tax_amount_cny      INTEGER NOT NULL DEFAULT 0,
    currency            CHAR(3) NOT NULL DEFAULT 'CNY',
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    payment_provider    VARCHAR(16),
    stripe_invoice_id   VARCHAR(64),
    alipay_trade_no     VARCHAR(64),
    buyer_name          VARCHAR(256),
    buyer_tax_id        VARCHAR(32),
    pdf_storage_path    TEXT,
    paid_at             TIMESTAMPTZ,
    due_at              TIMESTAMPTZ,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_invoices_number UNIQUE (number)
);

CREATE INDEX IF NOT EXISTS idx_invoices_tenant ON billing.invoices (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS billing.refresh_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES billing.users(id) ON DELETE CASCADE,
    token_hash      VARCHAR(64) NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_refresh_token_hash UNIQUE (token_hash)
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON billing.refresh_tokens (user_id);

CREATE TABLE IF NOT EXISTS billing.webhook_events (
    id              BIGSERIAL PRIMARY KEY,
    provider        VARCHAR(16) NOT NULL,
    external_id     VARCHAR(128) NOT NULL,
    event_type      VARCHAR(64) NOT NULL,
    payload         JSONB NOT NULL,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_webhook_external UNIQUE (provider, external_id)
);
