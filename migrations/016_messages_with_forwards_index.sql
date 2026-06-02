CREATE INDEX IF NOT EXISTS idx_messages_with_forwards
    ON messages (provider, account_id, id)
    WHERE has_forwards = 1;
