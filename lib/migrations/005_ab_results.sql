-- Migration 005 · ab_results
-- Owner: jobs.ab_aggregator (V3 Q3 · PRD §2.7 + §7)
-- One row per (utm_campaign, utm_term) arm. ``verdict`` captures the
-- two-proportion z-test outcome relative to the campaign's baseline arm
-- (defined as the arm with the largest sample_size; ties broken by term name).
CREATE TABLE IF NOT EXISTS ab_results (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    utm_campaign             TEXT NOT NULL,
    utm_term                 TEXT NOT NULL,
    sample_size              INTEGER NOT NULL,
    impressions_avg          REAL,
    ctr_mean                 REAL,
    ctr_lift_vs_baseline     REAL,                                -- +0.15 = 15% better
    p_value                  REAL,
    verdict                  TEXT NOT NULL CHECK(verdict IN (
                                'winner','loser','directional','noise','baseline'
                             )),
    computed_at              DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ab_results_campaign ON ab_results(utm_campaign);
CREATE INDEX IF NOT EXISTS idx_ab_results_computed ON ab_results(computed_at);
