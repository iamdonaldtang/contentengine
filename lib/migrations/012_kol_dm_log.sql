-- Migration 012 · kol_dm_log · KOL relationship state-machine log (T-03)
--
-- Background (B3 §1.4 自然回流):
--   The kol_watchlist table tracks Donald's static curation (Tier A/B,
--   focus areas) but says nothing about per-DM history. To close the loop
--   between "Donald hand-sent a DM/Reply" and "did the KOL respond / quote
--   us", we need a per-DM audit log. That's this table.
--
--   The state-machine progression (B3 §1.4):
--
--       sent (Donald logs DM via CLI)
--         │
--         │ kol_relation_tracker cron polls X API (daily 09:01)
--         ▼
--       replied (KOL responded in-thread within 7 days)
--         │
--         │ Donald observes 3+ Quote-tweets from same KOL within 90 days
--         ▼
--       quoted_repeatedly (tier auto-upgrade B → A in kol_watchlist)
--
-- This log is the *input* to a tier-upgrade decision that updates
-- kol_watchlist.tier / .relationship_status. The log itself is append-only
-- + replied_at UPDATE-once (idempotent via UPDATE ... WHERE kol_replied_at
-- IS NULL).
--
-- INDEPENDENCE:
--   kol_dm_log is part of the KOL side-chain (cron `1 9 * * *`, NOT in the
--   Sunday → Thursday publish chain). The migration creates the table even
--   if the cron is disabled; manual CLI logging works without the cron.
--   Schema reference is one-way: kol_dm_log → kol_watchlist (soft FK on
--   handle string; missing rows are tolerated since kol_watchlist may be
--   sparsely populated).
--
-- Storage convention:
--   * Timestamps are SQLite-native ``YYYY-MM-DD HH:MM:SS`` UTC strings
--     (set by CURRENT_TIMESTAMP) so cron queries can use
--     ``WHERE sent_at > datetime('now','-7 days')`` directly.
--   * donald_tweet_id is the X-native id parsed from donald_tweet_url for
--     cheap O(1) joining; both columns are stored (URL for human display,
--     id for API lookup).
--
-- Idempotency: schema_migrations gates re-runs.

CREATE TABLE IF NOT EXISTS kol_dm_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    kol_handle            TEXT NOT NULL,                                  -- "@handle" form, soft ref to kol_watchlist.handle
    piece_id              TEXT,                                            -- the piece this DM is about; NULL for ad-hoc DMs
    kind                  TEXT NOT NULL CHECK(kind IN (
                              'reply', 'dm', 'quote', 'custom_slice'
                          )),
    donald_tweet_url      TEXT,                                            -- Donald's public reply/quote URL on X (NULL for plain DMs)
    donald_tweet_id       TEXT,                                            -- X-native tweet id (parsed from URL)
    notes                 TEXT,                                            -- free-text context Donald can leave when logging
    sent_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,     -- when Donald sent it (UTC)
    kol_replied_at        DATETIME,                                        -- when KOL responded (NULL until tracker finds a reply)
    kol_reply_tweet_id    TEXT,                                            -- the reply's tweet id, if any
    kol_quote_count       INTEGER NOT NULL DEFAULT 0,                      -- count of separate Quote-tweets the KOL made of Donald's tweet
    last_checked_at       DATETIME,                                        -- when tracker last polled X for this row
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Index for the cron's hot scan: "find unreplied DMs in the last 7 days".
CREATE INDEX IF NOT EXISTS idx_kol_dm_log_unreplied
    ON kol_dm_log(kol_replied_at, sent_at);

-- Index for the tier-upgrade aggregator: "count Quote-tweets per KOL in last 90 days".
CREATE INDEX IF NOT EXISTS idx_kol_dm_log_handle
    ON kol_dm_log(kol_handle, sent_at);
