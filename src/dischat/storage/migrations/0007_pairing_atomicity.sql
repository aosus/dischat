-- Preserve only the newest active session per Matrix user before enforcing
-- the invariant for concurrent /pair requests.
WITH ranked AS (
    SELECT id, row_number() OVER (PARTITION BY mxid ORDER BY created_at DESC, id DESC) AS rn
    FROM pairing_sessions
    WHERE consumed_at IS NULL
)
UPDATE pairing_sessions
SET consumed_at = NOW()
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

CREATE UNIQUE INDEX IF NOT EXISTS pairing_sessions_one_active_mxid_idx
ON pairing_sessions (mxid) WHERE consumed_at IS NULL;

-- Timestamped issuance events implement a true rolling window. The summary
-- counters remain for compatibility and cooldown state.
CREATE TABLE IF NOT EXISTS pairing_issuance_events (
    id BIGSERIAL PRIMARY KEY,
    mxid TEXT NOT NULL,
    discourse_username TEXT NOT NULL DEFAULT '',
    issued_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS pairing_issuance_events_scope_time_idx
ON pairing_issuance_events (mxid, discourse_username, issued_at);
