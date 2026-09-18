-- =============================================================
-- Stripe webhook idempotency table
-- Stores processed Stripe event IDs to prevent duplicate processing
-- on Stripe retries.  Run once against your Supabase project.
-- =============================================================

CREATE TABLE IF NOT EXISTS stripe_events (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    stripe_event_id TEXT        UNIQUE NOT NULL,
    -- TW-079: status-gated idempotency. Only "processed" events are skipped;
    -- "failed" events are reprocessed on Stripe retry.
    -- NOTE (TW-088): the DEFAULT 'failed' is deliberate fail-open. Rows
    -- recorded under the old insert-before-dispatch code may have failed
    -- mid-dispatch; backfilling them as 'failed' lets Stripe retries (within
    -- its ~3-day window) reprocess them. All _dispatch handlers are
    -- idempotent upserts, so reprocessing is safe. Writers must still set
    -- status explicitly on insert.
    status          TEXT        NOT NULL DEFAULT 'failed',
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast duplicate check
CREATE INDEX IF NOT EXISTS idx_stripe_events_event_id ON stripe_events (stripe_event_id);
