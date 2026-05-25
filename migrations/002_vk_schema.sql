-- Replace scaffolded schema with VK-specific tables.
-- Safe drop: scaffold is empty in any environment that hasn't run parse-dump for real yet.

DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS chats;
DROP TABLE IF EXISTS users;

CREATE TABLE chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_folder TEXT NOT NULL,
    title TEXT,
    peer_id TEXT,
    created_at TIMESTAMP,
    last_message_at TIMESTAMP,
    last_message_text TEXT,
    last_message_sender TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    parsed_message_count INTEGER NOT NULL DEFAULT 0,
    total_expected_count INTEGER,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_parsed_at TIMESTAMP,
    UNIQUE (provider, account_id, source_folder)
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    vk_id INTEGER,
    display_name TEXT,
    profile_url TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (provider, vk_id)
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    vk_message_id INTEGER NOT NULL,
    sender_user_id INTEGER REFERENCES users(id),
    sender_vk_id INTEGER,
    sender_display_name TEXT,
    sender_is_self INTEGER NOT NULL DEFAULT 0,
    sent_at TIMESTAMP NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    has_forwards INTEGER NOT NULL DEFAULT 0,
    forwarded_count INTEGER NOT NULL DEFAULT 0,
    is_reply INTEGER NOT NULL DEFAULT 0,
    reply_to_message_id INTEGER,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    source_file TEXT NOT NULL,
    raw_html TEXT,
    UNIQUE (provider, account_id, chat_id, vk_message_id)
);

CREATE INDEX idx_messages_chat ON messages(chat_id, sent_at);
CREATE INDEX idx_messages_sender ON messages(sender_user_id);
CREATE INDEX idx_messages_vk_id ON messages(provider, account_id, vk_message_id);

CREATE TABLE attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    description TEXT,
    url TEXT,
    forward_count INTEGER,
    position INTEGER NOT NULL
);

CREATE INDEX idx_attachments_message ON attachments(message_id);
CREATE INDEX idx_attachments_kind ON attachments(kind);

CREATE TABLE parse_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    chat_id INTEGER REFERENCES chats(id),
    source_folder TEXT,
    source_file TEXT,
    raw_html TEXT,
    error TEXT,
    traceback TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_parse_errors_chat ON parse_errors(chat_id);
CREATE INDEX idx_parse_errors_run ON parse_errors(run_id)
