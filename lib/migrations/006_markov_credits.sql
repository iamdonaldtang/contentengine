-- Migration 006 · markov_credits
-- Owner: jobs.channel_attribution (V3 Q3 · PRD §2.7 + §7)
-- Multi-touch data-driven attribution via Markov-chain removal effect.
-- One row per (period, channel) pair. ``channel`` is "utm_source/utm_medium"
-- (e.g. "twitter/thread"); START and CONVERSION pseudo-states are NOT stored.
CREATE TABLE IF NOT EXISTS markov_credits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    period              TEXT NOT NULL,            -- e.g. 2026-05
    channel             TEXT NOT NULL,            -- "source/medium"
    removal_effect      REAL NOT NULL,
    credit_share        REAL NOT NULL,            -- 0.0..1.0; sums to 1.0 over a period
    conversion_credit   REAL NOT NULL,            -- credit_share * total_conversions
    sample_size         INTEGER NOT NULL,         -- distinct journeys touching channel
    computed_at         DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_markov_credits_period  ON markov_credits(period);
CREATE INDEX IF NOT EXISTS idx_markov_credits_channel ON markov_credits(channel);
