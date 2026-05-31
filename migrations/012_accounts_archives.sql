CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    vk_id INTEGER NOT NULL,
    display_name TEXT,
    avatar_url TEXT,
    avatar_local_path TEXT,
    UNIQUE (provider, vk_id)
);

CREATE TABLE archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    kind TEXT NOT NULL CHECK (kind IN ('folder','zip')),
    path TEXT NOT NULL,
    path_is_absolute INTEGER NOT NULL DEFAULT 1,
    lang TEXT NOT NULL DEFAULT 'unknown' CHECK (lang IN ('ru','en','unknown')),
    signature_text TEXT,
    generated_at TIMESTAMP,
    generation_duration_seconds REAL,
    source_timezone TEXT NOT NULL DEFAULT 'UTC',
    imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP,
    UNIQUE (account_id, generated_at, generation_duration_seconds),
    UNIQUE (account_id, path)
);

CREATE INDEX idx_archives_account ON archives(account_id);

ALTER TABLE chats DROP COLUMN source_timezone;
ALTER TABLE chats DROP COLUMN dump_generated_at;
