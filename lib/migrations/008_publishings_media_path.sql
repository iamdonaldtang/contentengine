-- Migration 008 · publishings.media_path — local mp4 / image file path.
--
-- Background (B1 §2.6/2.7, T4):
--   Short video output from MoneyPrinterTurbo lands at
--   ``runtime/drafts/<piece_id>/shorts_60s.mp4``. The schedule_planner and
--   downstream cron need a way to map "this publishings row carries that
--   media file" without re-deriving the path from piece_id + platform.
--
--   We store the host-side absolute path so Cowork artifact / human can
--   `start` the file directly. For uploads to platforms with API media
--   support (Postiz, YouTube), the path is the source; for Postiz-uploaded
--   media, also keep the Postiz media URL in raw_data / settings (caller's
--   responsibility).
--
-- Idempotency: schema_migrations gates re-runs. SQLite ALTER TABLE does not
-- support IF NOT EXISTS, so the migration runner is the only safeguard.

ALTER TABLE publishings ADD COLUMN media_path TEXT;
