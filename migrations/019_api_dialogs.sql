CREATE TABLE api_dialogs (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    peer_id INTEGER NOT NULL,
    peer_type TEXT NOT NULL,
    title TEXT,
    conversation_data TEXT NOT NULL,
    identity_data TEXT,
    latest_message_id INTEGER,
    message_count INTEGER,
    first_message_at INTEGER,
    last_message_at INTEGER,
    history_anchor_id INTEGER,
    history_status TEXT NOT NULL DEFAULT 'pending',
    discovery_id TEXT NOT NULL,
    discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, peer_id)
);

CREATE TABLE api_history_pages (
    account_id INTEGER NOT NULL,
    peer_id INTEGER NOT NULL,
    anchor_id INTEGER NOT NULL,
    page_offset INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    PRIMARY KEY (account_id, peer_id, anchor_id, page_offset),
    FOREIGN KEY (account_id, peer_id) REFERENCES api_dialogs(account_id, peer_id)
);

CREATE TABLE api_history_items (
    account_id INTEGER NOT NULL,
    peer_id INTEGER NOT NULL,
    anchor_id INTEGER NOT NULL,
    vk_message_id INTEGER NOT NULL,
    page_offset INTEGER NOT NULL,
    PRIMARY KEY (account_id, peer_id, anchor_id, vk_message_id),
    FOREIGN KEY (account_id, peer_id) REFERENCES api_dialogs(account_id, peer_id)
);

CREATE INDEX idx_api_history_items_page
    ON api_history_items(account_id, peer_id, anchor_id, page_offset);
