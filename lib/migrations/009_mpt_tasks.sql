-- Migration 009 · mpt_tasks · async MoneyPrinterTurbo render task tracker
--
-- Background (todo.md §J + A-design 2026-05-16):
--   Replaces the sync poll-block model where ``jobs.mpt_runner.run()`` blocked
--   the engine main thread for 5-10 minutes per render. New flow:
--     1. ``mpt_runner`` inserts a row (status='pending_submit') BEFORE calling
--        the MPT REST API. This gives reconciler something to find even if
--        the submit POST hangs / crashes.
--     2. After MPT returns ``task_id``, mpt_runner UPDATEs the row to
--        status='submitted' + records ``task_id`` and exits.
--     3. MPT renders asynchronously. On completion it POSTs to
--        ``/api/mpt-callback`` with an HMAC-SHA256 signed body. The engine
--        handler atomically transitions status='submitted' → 'completed'
--        and spawns a daemon thread to download the mp4 + stamp media_path.
--     4. If the callback is dropped (network blip, engine restart, MPT
--        crashed after rendering but before POSTing), ``jobs.mpt_reconciler``
--        cron runs every 5min and GETs MPT for any row stuck in 'submitted'
--        for > 5min. If MPT reports a terminal state, the reconciler
--        simulates the callback path — same atomic transition,
--        idempotent, no duplicate work.
--
-- State machine (write-once; idempotent updates use WHERE status='<expected>'):
--
--     pending_submit ─► submitted ─► completed   (callback OR reconciler wins race)
--                  │              ├► failed
--                  │              └► stale       (>6h, reconciler-only)
--                  └► failed                      (MPT submit POST errored before task_id assigned)
--
-- Idempotency rationale:
--   mark_completed / mark_failed / mark_stale all UPDATE ... WHERE status='submitted'.
--   Whoever runs first transitions the row; subsequent attempts touch 0 rows.
--   ``cursor.rowcount > 0`` is the synchronization primitive: the winning
--   caller proceeds to download mp4 / trigger downstream, the losers no-op.
--   This handles three race scenarios uniformly:
--     a) MPT retried the callback 2× (network) — second POST is a no-op.
--     b) reconciler GET MPT mid-flight just as the real callback arrives —
--        whichever atomic UPDATE wins, the other returns 0 rowcount.
--     c) engine restart after callback was processed but before media_path
--        was stamped — reconciler will re-trigger download (media_path is
--        stamped unconditionally; idempotent at filesystem level: same path).
--
-- Indexes:
--   * idx_mpt_tasks_task_id (partial UNIQUE) — callback handler lookup by
--     task_id; partial WHERE skips the NULL window between create_pending
--     and mark_submitted so multiple in-flight submits don't collide.
--   * idx_mpt_tasks_status_submitted — reconciler scan
--     ``WHERE status IN ('submitted','pending_submit') AND submitted_at < ?``.
--   * idx_mpt_tasks_piece — admin / debug ("show me all renders for piece X").
--
-- Idempotent migration: CREATE TABLE / INDEX IF NOT EXISTS plus
-- schema_migrations tracking in lib.db._run_migrations.

CREATE TABLE IF NOT EXISTS mpt_tasks (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id               TEXT,                                       -- MPT-assigned uuid; NULL until mark_submitted
    piece_id              TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending_submit',     -- see state machine docs above
    mp4_url               TEXT,                                       -- written by callback OR reconciler
    media_path            TEXT,                                       -- local fs path after download_video completes
    error                 TEXT,                                       -- last error message (set on terminal failure / stale)
    submit_attempt        INTEGER NOT NULL DEFAULT 0,
    terminal_source       TEXT,                                       -- 'callback' | 'reconciler' | NULL (still in-flight)
    submitted_at          DATETIME,                                   -- when MPT accepted submit (got task_id)
    callback_received_at  DATETIME,                                   -- when /api/mpt-callback fired (NULL if reconciler-won)
    completed_at          DATETIME,                                   -- terminal transition time
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (piece_id) REFERENCES pieces(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mpt_tasks_task_id
    ON mpt_tasks(task_id)
    WHERE task_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mpt_tasks_status_submitted
    ON mpt_tasks(status, submitted_at);

CREATE INDEX IF NOT EXISTS idx_mpt_tasks_piece
    ON mpt_tasks(piece_id);
