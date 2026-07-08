-- ============================================================================
-- Cerberus Platform — PostgreSQL Schema
-- Telemetry, Flywheel Logging, LLM-as-Judge Scoring, Fine-Tune Export
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ----------------------------------------------------------------------------
-- Tenants (multi-tenant isolation root)
-- ----------------------------------------------------------------------------
CREATE TABLE tenants (
    tenant_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                TEXT NOT NULL,
    tier                TEXT NOT NULL DEFAULT 'standard',   -- standard | enterprise | regulated
    isolation_mode      TEXT NOT NULL DEFAULT 'soft',       -- soft | hard (dedicated vector collection)
    data_residency      TEXT DEFAULT 'us',                  -- compliance routing hint
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    settings            JSONB NOT NULL DEFAULT '{}'::jsonb  -- e.g. custom thresholds, retention policy
);

-- ----------------------------------------------------------------------------
-- Requests: every inbound call to the gateway (pre-cache-decision record)
-- ----------------------------------------------------------------------------
CREATE TABLE requests (
    request_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    api_key_id          UUID,
    model_requested     TEXT,                -- model alias requested by client
    prompt_hash         TEXT NOT NULL,       -- sha256 of normalized prompt (dedup key)
    prompt_char_len     INT,
    prefix_depth_chars  INT DEFAULT 0,       -- how much matched the radix trie
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (created_at);

-- Monthly partitions for scale; create via a scheduled job.
CREATE TABLE requests_2026_07 PARTITION OF requests
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

CREATE INDEX idx_requests_tenant_time ON requests (tenant_id, created_at DESC);
CREATE INDEX idx_requests_prompt_hash ON requests (prompt_hash);

-- ----------------------------------------------------------------------------
-- Responses: outcome of each request — cache hit/miss + provider used
-- ----------------------------------------------------------------------------
CREATE TABLE responses (
    response_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    request_id          UUID NOT NULL REFERENCES requests(request_id),
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    cache_source        TEXT NOT NULL DEFAULT 'miss',  -- 'prefix' | 'semantic' | 'miss'
    cache_similarity    REAL,                          -- null on miss
    cache_vector_id     TEXT,                          -- pointer into Qdrant, null on miss
    provider            TEXT,                          -- 'openai' | 'anthropic' | 'self_hosted_llama' | null if cache hit
    model_used          TEXT,
    prompt_tokens       INT,
    completion_tokens   INT,
    cost_usd            NUMERIC(12,6),
    latency_ms          REAL,
    response_s3_uri     TEXT,                          -- raw payload archive pointer (large responses off-row)
    response_preview    TEXT,                          -- truncated preview for quick queries/dashboards
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (created_at);

CREATE TABLE responses_2026_07 PARTITION OF responses
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

CREATE INDEX idx_responses_tenant_time ON responses (tenant_id, created_at DESC);
CREATE INDEX idx_responses_cache_source ON responses (cache_source);

-- ----------------------------------------------------------------------------
-- LLM-as-Judge scores: async quality evaluation of responses
-- ----------------------------------------------------------------------------
CREATE TABLE judge_scores (
    judge_score_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    response_id          UUID NOT NULL REFERENCES responses(response_id),
    tenant_id            UUID NOT NULL REFERENCES tenants(tenant_id),
    judge_model           TEXT NOT NULL,        -- e.g. 'claude-haiku-4-5-20251001'
    overall_score          REAL NOT NULL,       -- normalized 0.0 - 1.0
    helpfulness_score      REAL,
    correctness_score      REAL,
    safety_flag            BOOLEAN NOT NULL DEFAULT FALSE,
    rationale               TEXT,               -- short judge explanation (for audit, not for training)
    evaluated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_judge_scores_response ON judge_scores (response_id);
CREATE INDEX idx_judge_scores_overall ON judge_scores (overall_score DESC);

-- ----------------------------------------------------------------------------
-- User feedback signal (explicit thumbs up/down — feeds the bandit threshold
-- controller and DPO pair mining)
-- ----------------------------------------------------------------------------
CREATE TABLE user_feedback (
    feedback_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    response_id          UUID NOT NULL REFERENCES responses(response_id),
    tenant_id            UUID NOT NULL REFERENCES tenants(tenant_id),
    signal               TEXT NOT NULL,   -- 'thumbs_up' | 'thumbs_down' | 'regenerate_requested'
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Cache security events: rejected writes, drift flags, injection markers
-- ----------------------------------------------------------------------------
CREATE TABLE cache_security_events (
    event_id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id            UUID NOT NULL REFERENCES tenants(tenant_id),
    request_id           UUID REFERENCES requests(request_id),
    event_type           TEXT NOT NULL,  -- 'write_rejected' | 'embedding_drift' | 'injection_detected'
    reason               TEXT,
    severity              TEXT NOT NULL DEFAULT 'low',  -- low | medium | high | critical
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_security_events_tenant ON cache_security_events (tenant_id, created_at DESC);

-- ----------------------------------------------------------------------------
-- Fine-tune export batches: tracks generated SFT/DPO JSONL artifacts
-- ----------------------------------------------------------------------------
CREATE TABLE finetune_export_batches (
    batch_id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id            UUID REFERENCES tenants(tenant_id),  -- null = platform-wide export
    export_type          TEXT NOT NULL,   -- 'sft' | 'dpo'
    min_judge_score       REAL NOT NULL,
    record_count          INT NOT NULL,
    s3_uri                TEXT NOT NULL,
    format_version         TEXT NOT NULL DEFAULT 'v1',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Row-level record of exactly which responses went into which export batch
CREATE TABLE finetune_export_records (
    id                    BIGSERIAL PRIMARY KEY,
    batch_id              UUID NOT NULL REFERENCES finetune_export_batches(batch_id),
    response_id           UUID NOT NULL REFERENCES responses(response_id),
    role_in_batch         TEXT NOT NULL DEFAULT 'sft_example'  -- 'sft_example' | 'dpo_chosen' | 'dpo_rejected'
);

-- ----------------------------------------------------------------------------
-- Row-Level Security: enforce tenant isolation at the database layer itself
-- (defense in depth beyond application-layer filters)
-- ----------------------------------------------------------------------------
ALTER TABLE requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE judge_scores ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_requests ON requests
    USING (tenant_id = current_setting('app.current_tenant_id')::uuid);

CREATE POLICY tenant_isolation_responses ON responses
    USING (tenant_id = current_setting('app.current_tenant_id')::uuid);

CREATE POLICY tenant_isolation_judge_scores ON judge_scores
    USING (tenant_id = current_setting('app.current_tenant_id')::uuid);

-- Application sets this per-connection/per-request before running queries:
--   SET app.current_tenant_id = '<uuid>';

-- ----------------------------------------------------------------------------
-- Convenience view: cost & cache-savings dashboard source
-- ----------------------------------------------------------------------------
CREATE VIEW v_tenant_daily_savings AS
SELECT
    r.tenant_id,
    date_trunc('day', r.created_at) AS day,
    count(*) FILTER (WHERE r.cache_source != 'miss') AS cache_hits,
    count(*) FILTER (WHERE r.cache_source = 'miss') AS cache_misses,
    sum(r.cost_usd) FILTER (WHERE r.cache_source = 'miss') AS actual_spend_usd,
    -- Estimated cost if every hit had instead been a miss at the tenant's
    -- trailing average miss cost — a simple, explainable savings estimate.
    (count(*) FILTER (WHERE r.cache_source != 'miss'))::numeric
        * (avg(r.cost_usd) FILTER (WHERE r.cache_source = 'miss')) AS estimated_savings_usd
FROM responses r
GROUP BY r.tenant_id, date_trunc('day', r.created_at);
