CREATE INDEX IF NOT EXISTS idx_attachments_failed_recent
    ON attachments (download_attempted_at)
    WHERE download_status = 'failed';
