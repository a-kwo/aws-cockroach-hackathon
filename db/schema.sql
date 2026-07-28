-- Brass Tacks — CockroachDB memory layer
--
-- Design notes:
--   * Money is ALWAYS integer cents (BIGINT). Never floats, never DECIMAL-as-float.
--   * Vector indexes use a `business_id` prefix column so tenant filtering happens
--     inside the index rather than as a post-filter.
--   * Embeddings are 1024-dim (Amazon Titan Text Embeddings V2 default output).
--   * Every agent action writes an `agent_run` row. The audit trail IS the product.
--
-- Requires: CockroachDB with vector index support enabled. See 00_cluster_settings.sql.

BEGIN;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE IF NOT EXISTS agent_kind AS ENUM (
  'mapper',    -- chat -> business profile
  'radar',     -- nightly observation sweep
  'analyst',   -- retrieval + reasoning -> a find with a prediction
  'maker',     -- produces the done-for-you artifact
  'meter',     -- verifies prior predictions against outcomes
  'ask'        -- owner Q&A, read-only, over the Cockroach MCP server
);

CREATE TYPE IF NOT EXISTS run_status AS ENUM ('running', 'ok', 'failed');

CREATE TYPE IF NOT EXISTS observation_kind AS ENUM (
  'review',        -- a customer review
  'rival_price',   -- a competitor's price point
  'rival_menu',    -- a competitor's menu / offering
  'trend',         -- search or foot-traffic trend signal
  'social',        -- forum / social mention
  'owner_upload'   -- something the owner handed us
);

CREATE TYPE IF NOT EXISTS fact_source AS ENUM (
  'owner_chat',    -- the owner told us directly ("what only the owner knows")
  'inferred',      -- the agent concluded it
  'observed'       -- derived from an observation
);

CREATE TYPE IF NOT EXISTS find_status AS ENUM (
  'proposed',      -- analyst surfaced it, owner hasn't decided
  'accepted',      -- owner said do it now
  'later',         -- owner put it in the Later jar
  'rejected',      -- owner declined
  'live',          -- shipped; the meter is watching it
  'retired'        -- pulled after the fact (e.g. it was a miss)
);

CREATE TYPE IF NOT EXISTS ledger_verdict AS ENUM (
  'verified',      -- measured against real outcome data
  'estimated',     -- modeled; not yet verifiable
  'miss'           -- it did not pay off. We publish these.
);

-- ---------------------------------------------------------------------------
-- Tenant + profile
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS business (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         STRING NOT NULL,
  category     STRING NOT NULL,              -- 'restaurant', 'salon', ...
  city         STRING,
  region       STRING,
  goal_monthly_cents BIGINT,                 -- the locked goal, e.g. +$8,000/mo
  goal_note    STRING,                       -- 'by fall'
  created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- "The leash you hold" — constraints the autopilot obeys on every run.
CREATE TABLE IF NOT EXISTS owner_rule (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id  UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  rule         STRING NOT NULL,
  enabled      BOOL NOT NULL DEFAULT true,
  cap_cents    BIGINT,                       -- optional spend cap for this rule
  created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  INDEX (business_id, enabled)
);

-- Profile memory: durable facts about the business. Facts change over time, so
-- they are superseded rather than mutated — the history is part of the memory.
CREATE TABLE IF NOT EXISTS business_fact (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id   UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  fact          STRING NOT NULL,
  source        fact_source NOT NULL,
  confidence    FLOAT NOT NULL DEFAULT 1.0,
  learned_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  superseded_by UUID REFERENCES business_fact(id),
  embedding     VECTOR(1024),

  INDEX (business_id, superseded_by),
  VECTOR INDEX business_fact_embed_idx (business_id, embedding vector_cosine_ops)
);

-- ---------------------------------------------------------------------------
-- Audit trail
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_run (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id   UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  agent         agent_kind NOT NULL,
  status        run_status NOT NULL DEFAULT 'running',
  started_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  finished_at   TIMESTAMPTZ,
  model_id      STRING,                      -- e.g. the Bedrock model used
  input_tokens  INT,
  output_tokens INT,
  error         STRING,
  note          STRING,

  INDEX (business_id, agent, started_at DESC)
);

-- ---------------------------------------------------------------------------
-- Radar: what the agents observed
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS observation (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id   UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  run_id        UUID REFERENCES agent_run(id),
  kind          observation_kind NOT NULL,
  content       STRING NOT NULL,             -- the text that gets embedded
  source_name   STRING,                      -- 'review site', 'forum', ...
  source_url    STRING,
  subject       STRING,                      -- which rival / which dish
  rating        FLOAT,                       -- for reviews, if present
  observed_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  ingested_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

  -- Radar runs nightly and WILL re-see the same content. Dedup is part of the
  -- design, not an optimization.
  content_hash  STRING NOT NULL,

  embedding     VECTOR(1024),

  UNIQUE INDEX observation_dedup_idx (business_id, content_hash),
  INDEX (business_id, kind, observed_at DESC),
  VECTOR INDEX observation_embed_idx (business_id, embedding vector_cosine_ops)
);

-- ---------------------------------------------------------------------------
-- Analyst: a find, and — critically — its prediction
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS find (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id           UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  run_id                UUID REFERENCES agent_run(id),

  emoji                 STRING,
  title                 STRING NOT NULL,     -- 'Tiramisu -> $9'
  rationale             STRING NOT NULL,     -- why the agent believes it
  move                  STRING,              -- what we will actually do

  -- The prediction. This is what makes the Meter possible.
  predicted_daily_cents BIGINT NOT NULL,
  confidence            FLOAT NOT NULL,
  verify_after          DATE NOT NULL,       -- do not judge this before then

  status                find_status NOT NULL DEFAULT 'proposed',
  decided_at            TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

  INDEX (business_id, status, verify_after),
  INDEX (business_id, created_at DESC)
);

-- Which observations the vector search actually returned for this find, and how
-- close they were. This is the receipt proving retrieval drove the reasoning.
CREATE TABLE IF NOT EXISTS find_evidence (
  find_id        UUID NOT NULL REFERENCES find(id) ON DELETE CASCADE,
  observation_id UUID NOT NULL REFERENCES observation(id) ON DELETE CASCADE,
  similarity     FLOAT NOT NULL,             -- 1 - cosine_distance, at query time
  rank           INT NOT NULL,               -- position in the retrieved set

  PRIMARY KEY (find_id, observation_id)
);

-- ---------------------------------------------------------------------------
-- Maker: the done-for-you artifact
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS artifact (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  find_id      UUID NOT NULL REFERENCES find(id) ON DELETE CASCADE,
  run_id       UUID REFERENCES agent_run(id),
  kind         STRING NOT NULL,              -- 'menu', 'review_reply', 'plan'
  title        STRING NOT NULL,
  s3_bucket    STRING,
  s3_key       STRING,
  preview      STRING,                       -- inline preview for the UI
  created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

  INDEX (find_id)
);

-- ---------------------------------------------------------------------------
-- Meter: the ledger. Verified, estimated, and the honest misses.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ledger_entry (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id           UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  find_id               UUID NOT NULL REFERENCES find(id) ON DELETE CASCADE,
  run_id                UUID REFERENCES agent_run(id),

  verdict               ledger_verdict NOT NULL,

  -- Snapshot of what we predicted, so the ledger stays honest even if the find
  -- is later edited. A miss must remain a miss.
  predicted_daily_cents BIGINT NOT NULL,
  actual_daily_cents    BIGINT NOT NULL DEFAULT 0,

  measured_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  period_start          DATE NOT NULL,
  period_end            DATE NOT NULL,
  method                STRING NOT NULL,     -- how we measured it
  note                  STRING,

  -- One verdict per find per measurement period.
  UNIQUE INDEX ledger_period_idx (find_id, period_start, period_end),
  INDEX (business_id, measured_at DESC)
);

COMMIT;
