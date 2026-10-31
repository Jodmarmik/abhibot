CREATE TABLE IF NOT EXISTS af_settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS af_sudo (
    user_id BIGINT PRIMARY KEY CHECK (user_id > 0),
    granted_by BIGINT NOT NULL CHECK (granted_by > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS af_groups (
    chat_id BIGINT PRIMARY KEY CHECK (chat_id < 0),
    approved_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS af_channels (
    chat_id BIGINT NOT NULL CHECK (chat_id < 0),
    role TEXT NOT NULL CHECK (role IN ('source', 'storage')),
    added_by BIGINT NOT NULL,
    PRIMARY KEY (chat_id, role)
);
CREATE TABLE IF NOT EXISTS af_files (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL CHECK (message_id > 0),
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('document', 'video', 'audio')),
    size BIGINT NOT NULL CHECK (size >= 0),
    caption TEXT NOT NULL DEFAULT '',
    file_id TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS af_files_fingerprint ON af_files (fingerprint);
CREATE INDEX IF NOT EXISTS af_files_search ON af_files USING gin (to_tsvector('simple', normalized));
CREATE INDEX IF NOT EXISTS af_files_channel ON af_files (channel_id, active);
CREATE TABLE IF NOT EXISTS af_users (
    user_id BIGINT PRIMARY KEY CHECK (user_id > 0),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS af_metrics (
    key TEXT PRIMARY KEY,
    value BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS af_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS af_audit_created ON af_audit (created_at DESC);
