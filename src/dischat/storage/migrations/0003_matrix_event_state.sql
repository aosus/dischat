-- Durable state for restart-safe Matrix event processing (issue #9).
--
-- matrix_sync_state holds the Matrix /sync continuation token so the bridge
-- resumes from the last acknowledged batch instead of performing a fresh
-- initial sync after a restart.
--
-- matrix_event_state is the durable processing ledger ("claim fence") for
-- inbound Matrix events. The unique constraint on (room_id, event_id) makes
-- claiming an event idempotent: exactly one attempt can transition a marker
-- from 'claimed' to 'processed', and replays short-circuit on the unique
-- violation before any Discourse write happens.

CREATE TABLE IF NOT EXISTS matrix_sync_state (
    singleton BOOLEAN NOT NULL PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    next_batch TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS matrix_event_state (
    id BIGSERIAL PRIMARY KEY,
    room_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'claimed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (room_id, event_id)
);

CREATE INDEX IF NOT EXISTS matrix_event_state_status_idx
ON matrix_event_state (status);
