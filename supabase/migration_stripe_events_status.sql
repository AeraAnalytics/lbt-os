-- =============================================================
-- TW-079: status-gated Stripe webhook idempotency
-- Adds the status column to stripe_events for databases created
-- before the column existed. Run once against your Supabase project.
--
-- Only events marked 'processed' are treated as duplicates. A failed
-- dispatch is marked 'failed' so Stripe's retry reprocesses it instead
-- of being swallowed as a duplicate (the pre-TW-079 bug).
-- =============================================================

ALTER TABLE stripe_events
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'processed';
