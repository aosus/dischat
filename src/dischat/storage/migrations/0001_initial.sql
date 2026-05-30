CREATE TABLE IF NOT EXISTS chat_accounts (
    id BIGSERIAL PRIMARY KEY,
    mxid TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    discourse_user_id BIGINT NULL,
    discourse_username TEXT NULL,
    paired_at TIMESTAMPTZ NULL,
    revoked_at TIMESTAMPTZ NULL,
    notify_on_direct_replies BOOLEAN NOT NULL DEFAULT TRUE,
    notify_on_mentions BOOLEAN NOT NULL DEFAULT TRUE,
    response_locale TEXT NOT NULL DEFAULT 'ar',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pairing_sessions (
    id BIGSERIAL PRIMARY KEY,
    mxid TEXT NOT NULL,
    discourse_username TEXT NOT NULL,
    discourse_user_id BIGINT NULL,
    code_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS categories (
    id BIGSERIAL PRIMARY KEY,
    discourse_category_id BIGINT NOT NULL UNIQUE,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    is_public BOOLEAN NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_watches (
    id BIGSERIAL PRIMARY KEY,
    mxid TEXT NOT NULL,
    mode TEXT NOT NULL,
    category_id BIGINT NULL REFERENCES categories(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS room_links (
    id BIGSERIAL PRIMARY KEY,
    matrix_room_id TEXT NOT NULL UNIQUE,
    include_all_public_categories BOOLEAN NOT NULL DEFAULT FALSE,
    allow_relay BOOLEAN NOT NULL DEFAULT FALSE,
    full_content BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS room_link_categories (
    id BIGSERIAL PRIMARY KEY,
    room_link_id BIGINT NOT NULL REFERENCES room_links(id) ON DELETE CASCADE,
    category_id BIGINT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    UNIQUE (room_link_id, category_id)
);

CREATE TABLE IF NOT EXISTS discourse_events (
    id BIGSERIAL PRIMARY KEY,
    discourse_topic_id BIGINT NOT NULL,
    discourse_post_id BIGINT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    category_id BIGINT NULL,
    author_username TEXT NOT NULL,
    target_discourse_username TEXT NULL,
    raw_payload_json JSONB NOT NULL,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS delivery_jobs (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES discourse_events(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_mxid TEXT NULL,
    matrix_room_id TEXT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS delivery_messages (
    id BIGSERIAL PRIMARY KEY,
    discourse_topic_id BIGINT NOT NULL,
    discourse_post_id BIGINT NOT NULL,
    matrix_room_id TEXT NOT NULL,
    matrix_event_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_mxid TEXT NULL,
    parent_delivery_message_id BIGINT NULL REFERENCES delivery_messages(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (matrix_room_id, matrix_event_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    mxid TEXT NULL,
    platform TEXT NULL,
    discourse_username_used TEXT NOT NULL,
    discourse_user_id_used BIGINT NULL,
    topic_id BIGINT NULL,
    post_id BIGINT NULL,
    matrix_room_id TEXT NULL,
    matrix_event_id TEXT NULL,
    success BOOLEAN NOT NULL,
    error_message TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS delivery_jobs_dedup_idx
ON delivery_jobs (event_id, target_type, COALESCE(target_mxid, ''), COALESCE(matrix_room_id, ''));

CREATE UNIQUE INDEX IF NOT EXISTS delivery_messages_dedup_idx
ON delivery_messages (discourse_post_id, matrix_room_id, target_type, COALESCE(target_mxid, ''));
