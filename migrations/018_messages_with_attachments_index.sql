CREATE INDEX IF NOT EXISTS idx_messages_with_attachments
    ON messages (provider, account_id, id)
    WHERE attachment_count > 0;
