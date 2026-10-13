INSERT OR IGNORE INTO accounts (provider, vk_id)
SELECT DISTINCT provider, CAST(account_id AS INTEGER) FROM chats
WHERE account_id != '' AND account_id NOT GLOB '*[^0-9]*';
CREATE TABLE chats_new (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 provider TEXT NOT NULL,
 account_id INTEGER REFERENCES accounts(id),
 source_folder TEXT,
 title TEXT, peer_id TEXT, created_at TIMESTAMP, last_message_at TIMESTAMP,
 last_message_text TEXT, last_message_sender TEXT,
 message_count INTEGER NOT NULL DEFAULT 0, parsed_message_count INTEGER NOT NULL DEFAULT 0,
 total_expected_count INTEGER, error_count INTEGER NOT NULL DEFAULT 0,
 last_parsed_at TIMESTAMP, type TEXT,
 history_status TEXT NOT NULL DEFAULT 'partial',
 UNIQUE(provider, account_id, peer_id),
 UNIQUE(provider, account_id, source_folder)
);
INSERT INTO chats_new
SELECT c.id,c.provider,a.id,c.source_folder,c.title,c.peer_id,c.created_at,c.last_message_at,
 c.last_message_text,c.last_message_sender,c.message_count,c.parsed_message_count,
 c.total_expected_count,c.error_count,c.last_parsed_at,c.type,'partial'
FROM chats c LEFT JOIN accounts a ON a.provider=c.provider AND CAST(a.vk_id AS TEXT)=c.account_id;
CREATE TABLE messages_new (
 id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
 account_id INTEGER REFERENCES accounts(id), chat_id INTEGER NOT NULL REFERENCES chats_new(id),
 vk_message_id INTEGER NOT NULL, sender_user_id INTEGER REFERENCES users(id),
 sender_vk_id INTEGER, sender_display_name TEXT, sender_is_self INTEGER NOT NULL DEFAULT 0,
 sent_at TIMESTAMP NOT NULL, text TEXT NOT NULL DEFAULT '',
 has_forwards INTEGER NOT NULL DEFAULT 0, forwarded_count INTEGER NOT NULL DEFAULT 0,
 is_reply INTEGER NOT NULL DEFAULT 0, reply_to_message_id INTEGER,
 attachment_count INTEGER NOT NULL DEFAULT 0, source_file TEXT, raw_html TEXT,
 is_edited INTEGER NOT NULL DEFAULT 0, edited_at TIMESTAMP,
 conversation_message_id INTEGER, reply_data_json TEXT, source_kind TEXT NOT NULL DEFAULT 'archive',
 UNIQUE(chat_id,vk_message_id)
);
INSERT INTO messages_new (id,provider,account_id,chat_id,vk_message_id,sender_user_id,
 sender_vk_id,sender_display_name,sender_is_self,sent_at,text,has_forwards,forwarded_count,
 is_reply,reply_to_message_id,attachment_count,source_file,raw_html,is_edited,edited_at)
SELECT m.id,m.provider,c.account_id,m.chat_id,m.vk_message_id,m.sender_user_id,m.sender_vk_id,
 m.sender_display_name,m.sender_is_self,m.sent_at,m.text,m.has_forwards,m.forwarded_count,
 m.is_reply,m.reply_to_message_id,m.attachment_count,m.source_file,m.raw_html,m.is_edited,m.edited_at
FROM messages m JOIN chats_new c ON c.id=m.chat_id;
CREATE TABLE attachments_new (
 id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL REFERENCES messages_new(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, description TEXT, url TEXT, forward_count INTEGER, position INTEGER NOT NULL,
 local_path TEXT, download_status TEXT NOT NULL DEFAULT 'pending', download_attempted_at TIMESTAMP,
 download_error TEXT, file_size INTEGER, content_type TEXT, remote_modified_at TIMESTAMP,
 resolution TEXT, data_json TEXT, external_id TEXT, access_key TEXT,
 UNIQUE(message_id,position)
);
INSERT INTO attachments_new (id,message_id,kind,description,url,forward_count,position,local_path,
 download_status,download_attempted_at,download_error,file_size,content_type,remote_modified_at,resolution,data_json)
SELECT id,message_id,kind,description,url,forward_count,position,local_path,download_status,
 download_attempted_at,download_error,file_size,content_type,remote_modified_at,resolution,data_json FROM attachments;
CREATE TABLE parse_errors_new (
 id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER REFERENCES runs(id),chat_id INTEGER REFERENCES chats_new(id),
 source_folder TEXT,source_file TEXT,raw_html TEXT,error TEXT,traceback TEXT,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO parse_errors_new SELECT * FROM parse_errors;
DROP TABLE attachments;
DROP TABLE parse_errors;
DROP TABLE messages;
DROP TABLE chats;
ALTER TABLE chats_new RENAME TO chats;
ALTER TABLE messages_new RENAME TO messages;
ALTER TABLE attachments_new RENAME TO attachments;
ALTER TABLE parse_errors_new RENAME TO parse_errors;
CREATE INDEX idx_messages_chat ON messages(chat_id,sent_at);
CREATE INDEX idx_messages_sender ON messages(sender_user_id);
CREATE INDEX idx_messages_vk_id ON messages(provider,account_id,vk_message_id);
CREATE INDEX idx_messages_with_forwards ON messages(provider,account_id,id) WHERE has_forwards=1;
CREATE INDEX idx_messages_with_attachments ON messages(provider,account_id,id) WHERE attachment_count>0;
CREATE INDEX idx_attachments_message ON attachments(message_id);
CREATE INDEX idx_attachments_kind ON attachments(kind);
CREATE INDEX idx_attachments_download_status ON attachments(kind,download_status);
CREATE INDEX idx_attachments_failed_recent ON attachments(download_attempted_at) WHERE download_status='failed';
CREATE INDEX idx_parse_errors_chat ON parse_errors(chat_id);
CREATE INDEX idx_parse_errors_run ON parse_errors(run_id);
CREATE TABLE chat_data_issues (
 chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 vk_message_id INTEGER NOT NULL DEFAULT 0,
 source TEXT NOT NULL, field TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(chat_id,vk_message_id,source,field)
);
