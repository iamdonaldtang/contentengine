-- Migration 007 · cookie_email_map · anonymous-impression → signed-up-lead stitching.
--
-- Background (B1 §4.3, todo.md §C.2):
--   The landing-page JS persists a 30-day _taskon_uid cookie. Anonymous
--   impressions write user_journey rows keyed by that cookie_id. When the
--   user later submits a form (POST /api/landing-signup with email + same
--   cookie_id), ingestion captures the (cookie_id, sha256(email)) pair here
--   so attribution_engine can union the email_hash journey with all matching
--   cookie_id journeys when computing first/last/linear touch.
--
-- Schema rationale:
--   * Composite PK on (cookie_id, email_hash) — one user can collect multiple
--     cookies (cleared / different devices), one cookie can outlive multiple
--     emails. Both directions matter.
--   * first_seen / last_seen as integer unix seconds — cheap COMPARE for
--     "pick the most recent cookie" stitching queries, no timezone games.
--   * idx_cookie_email_email lets the email_hash → cookies lookup
--     (attribution_engine.lookup_cookies_for_email_hash) hit an index.
--
-- Idempotency: migration runner records version in schema_migrations and
-- skips re-application. CREATE TABLE / INDEX guard with IF NOT EXISTS for
-- defensive belt-and-braces against partial replays.

CREATE TABLE IF NOT EXISTS cookie_email_map (
    cookie_id  TEXT    NOT NULL,
    email_hash TEXT    NOT NULL,
    first_seen INTEGER NOT NULL,  -- unix seconds
    last_seen  INTEGER NOT NULL,  -- unix seconds
    PRIMARY KEY (cookie_id, email_hash)
);

CREATE INDEX IF NOT EXISTS idx_cookie_email_email  ON cookie_email_map(email_hash);
CREATE INDEX IF NOT EXISTS idx_cookie_email_cookie ON cookie_email_map(cookie_id);
