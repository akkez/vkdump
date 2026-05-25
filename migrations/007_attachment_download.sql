ALTER TABLE attachments ADD COLUMN local_path TEXT;
ALTER TABLE attachments ADD COLUMN download_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE attachments ADD COLUMN download_attempted_at TIMESTAMP;
ALTER TABLE attachments ADD COLUMN download_error TEXT;
ALTER TABLE attachments ADD COLUMN file_size INTEGER;
ALTER TABLE attachments ADD COLUMN content_type TEXT;
CREATE INDEX idx_attachments_download_status ON attachments(kind, download_status)
