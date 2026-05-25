CREATE TABLE chats (
    id INTEGER PRIMARY KEY,
    title TEXT,
    type TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    display_name TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER REFERENCES chats(id),
    sender_id INTEGER REFERENCES users(id),
    sent_at TIMESTAMP,
    text TEXT,
    raw_json TEXT
);

CREATE INDEX idx_messages_chat_id ON messages(chat_id);

CREATE INDEX idx_messages_sender_id ON messages(sender_id);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    params TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    result TEXT
);

CREATE INDEX idx_runs_started_at ON runs(started_at)
