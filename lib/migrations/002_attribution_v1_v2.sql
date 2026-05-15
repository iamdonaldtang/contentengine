-- Migration 002 · Attribution V1 (first-touch) + V2 (linear) parallel columns.
-- PRD §2.7 升级路径 + Metrics doc §4.3.
--
-- Adds two leads.* columns so we can carry first-touch and linear-attribution
-- payloads alongside the existing last-touch first_utm_* truth.
--
-- Idempotency: the migration is run exactly once because lib/db.py records the
-- applied version in schema_migrations. SQLite's ALTER TABLE does not support
-- IF NOT EXISTS, so we rely entirely on schema_migrations to prevent re-run.
-- If you need to apply this manually outside the migration runner, guard with
-- a pragma_table_info() check from your invoking code.

ALTER TABLE leads ADD COLUMN first_touch_piece_id TEXT;
ALTER TABLE leads ADD COLUMN linear_weights_json  TEXT;

-- Helpful index for downstream Performance Analyzer + weekly aggregates joins.
CREATE INDEX IF NOT EXISTS idx_leads_first_touch_piece ON leads(first_touch_piece_id);
