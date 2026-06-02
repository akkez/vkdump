CREATE TABLE api_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    peer_id INTEGER NOT NULL,
    vk_message_id INTEGER NOT NULL,
    message_data TEXT NOT NULL,
    attachment_data TEXT,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, vk_message_id)
);

CREATE INDEX idx_api_messages_account_peer
    ON api_messages(account_id, peer_id);
