CREATE TABLE IF NOT EXISTS pairing_rate_limits (
    id BIGSERIAL PRIMARY KEY,
    mxid TEXT NOT NULL,
    discourse_username TEXT NULL,
    issuance_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cooldown_until TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS pairing_rate_limits_mxid_username_idx
ON pairing_rate_limits (mxid, COALESCE(discourse_username, ''));
