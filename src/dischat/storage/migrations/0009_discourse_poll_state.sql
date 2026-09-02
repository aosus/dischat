CREATE TABLE IF NOT EXISTS discourse_poll_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    last_seen_post_id BIGINT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
