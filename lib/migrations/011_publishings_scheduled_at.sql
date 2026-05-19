-- Migration 011 · publishings.scheduled_at + composite index for alert crons
--
-- Background:
--   T-05 (reply density alert) and T-07 (LinkedIn engagement alert) trigger
--   30 min after a post is supposed to be live. publishings.published_at is
--   NULL until metrics_collector (daily 20:00) backfills it from Postiz
--   analytics — far past the 30-min nudge window. We need the Postiz-promised
--   publish time, set by schedule_planner at the moment the post enters
--   Postiz's queue.
--
-- Storage convention (must match how schedule_planner writes it):
--   ``YYYY-MM-DD HH:MM:SS`` UTC string. SQLite's ``CURRENT_TIMESTAMP`` /
--   ``datetime('now', ...)`` use the same format, so the cron's
--   ``WHERE scheduled_at BETWEEN datetime('now','-31 min') AND
--   datetime('now','-29 min')`` works as a lexicographic string compare.
--
-- Composite index supports the alert cron's hot path:
--   ``WHERE platform LIKE '...' AND <col>_alert_sent IS NULL
--    AND scheduled_at BETWEEN ? AND ?``
-- The index on (platform, scheduled_at) lets SQLite scan only the relevant
-- platform slice — cheap even as publishings grows.

ALTER TABLE publishings ADD COLUMN scheduled_at DATETIME;

CREATE INDEX IF NOT EXISTS idx_publishings_scheduled
    ON publishings(platform, scheduled_at);
