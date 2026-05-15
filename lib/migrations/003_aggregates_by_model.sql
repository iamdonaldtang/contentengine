-- Migration 003 · Stratify weekly_aggregates by attribution_model.
-- PRD §2.7 V1/V2 + Metrics doc §4.3 + §4.5.
--
-- Adds a single ``attribution_model`` column so the same (week, dimension,
-- value) tuple can carry one row per model (last_touch / first_touch / linear).
-- Existing rows keep the default 'last_touch' which matches the historical
-- single-model contract.
--
-- Idempotency: applied exactly once via schema_migrations tracking in lib/db.py.

ALTER TABLE weekly_aggregates ADD COLUMN attribution_model TEXT DEFAULT 'last_touch';

-- Composite lookup index for topic_ranker queries that read (week, dimension,
-- attribution_model) together.
CREATE INDEX IF NOT EXISTS idx_weekly_agg_week_dim_model
    ON weekly_aggregates(week, dimension, attribution_model);
