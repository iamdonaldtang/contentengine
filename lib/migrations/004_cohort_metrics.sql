-- Migration 004 · cohort_metrics
-- Owner: jobs.cohort_analysis (V3 Q3 · PRD §2.7 + §7)
-- Tracks lead cohorts by ISO signup week and the D7/D14/D30 conversion
-- progression through "qualified" and "sql_verified" milestones.
--
-- Primary key on cohort_week makes the writer idempotent: re-running the
-- analysis simply UPSERTs the row.
CREATE TABLE IF NOT EXISTS cohort_metrics (
    cohort_week     TEXT PRIMARY KEY,           -- e.g. 2026W18
    cohort_size     INTEGER NOT NULL,
    d7_qualified    INTEGER NOT NULL DEFAULT 0,
    d14_qualified   INTEGER NOT NULL DEFAULT 0,
    d30_qualified   INTEGER NOT NULL DEFAULT 0,
    d7_sql          INTEGER NOT NULL DEFAULT 0,
    d14_sql         INTEGER NOT NULL DEFAULT 0,
    d30_sql         INTEGER NOT NULL DEFAULT 0,
    d7_conv_rate    REAL NOT NULL DEFAULT 0.0,
    d30_conv_rate   REAL NOT NULL DEFAULT 0.0,
    computed_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cohort_metrics_week ON cohort_metrics(cohort_week);
