PRAGMA foreign_keys = ON;

CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT,
  parent_team_id TEXT,
  parent_team_name TEXT,
  thread_type TEXT,
  chat_sub_type TEXT,
  is_favorite INTEGER,
  is_collapsed INTEGER,
  is_general INTEGER,
  is_muted INTEGER,
  is_read INTEGER,
  is_empty_conversation INTEGER,
  remote_last_message_at TEXT,
  local_last_message_at TEXT,
  message_count INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE messages (
  conversation_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  parent_id TEXT,
  author TEXT,
  timestamp TEXT NOT NULL,
  content_markdown TEXT,
  kind TEXT,
  message_type TEXT,
  version TEXT,
  raw_json TEXT,
  first_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (conversation_id, message_id),
  FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE TABLE attachments (
  conversation_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  attachment_index INTEGER NOT NULL,
  attachment_id TEXT,
  kind TEXT NOT NULL,
  name TEXT,
  remote_url TEXT,
  local_path TEXT,
  content_type TEXT,
  raw_json TEXT,
  status TEXT,
  first_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (conversation_id, message_id, attachment_index),
  FOREIGN KEY (conversation_id, message_id)
    REFERENCES messages(conversation_id, message_id)
);

CREATE TABLE meetings (
  conversation_id TEXT PRIMARY KEY,
  subject TEXT,
  start_time TEXT,
  end_time TEXT,
  join_url TEXT,
  organizer_id TEXT,
  meeting_type TEXT,
  raw_json TEXT,
  first_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

INSERT INTO conversations VALUES (
  'conversation-1', 'meeting_chat', 'Review Meeting', NULL, NULL,
  'chat', 'meeting', 1, 0, 0, 0, 1, 0,
  '2026-01-02T10:10:00Z', '2026-01-02T10:10:00Z', 3,
  '{"topic":"Review Meeting"}',
  '2026-01-02T09:00:00Z', '2026-01-02T12:00:00Z',
  '2026-01-02T12:00:00Z'
);

INSERT INTO messages VALUES (
  'conversation-1', 'message-1', NULL, 'Actor A',
  '2026-01-02T10:00:00Z', 'Initial configuration question.',
  'message', 'text', '1', '{"authorId":"actor-a"}',
  '2026-01-02T10:00:00Z', '2026-01-02T10:00:00Z'
);

INSERT INTO messages VALUES (
  'conversation-1', 'message-2', NULL, 'Actor B',
  '2026-01-02T10:05:00Z', 'Use the green setting.',
  'message', 'text', '2', '{"authorId":"actor-b","edited":true}',
  '2026-01-02T10:04:00Z', '2026-01-02T10:06:00Z'
);

INSERT INTO messages VALUES (
  'conversation-1', 'message-3', NULL, 'Actor A',
  '2026-01-02T10:10:00Z', 'I will validate the setting.',
  'message', 'text', '1', '{"authorId":"actor-a"}',
  '2026-01-02T10:10:00Z', '2026-01-02T10:10:00Z'
);

INSERT INTO attachments VALUES (
  'conversation-1', 'message-3', 0, 'attachment-1', 'file',
  'validation.txt', 'https://example.invalid/validation.txt', NULL,
  'text/plain', '{"id":"attachment-1"}', 'remote',
  '2026-01-02T10:10:00Z', '2026-01-02T10:10:00Z'
);

INSERT INTO meetings VALUES (
  'conversation-1', 'Review Meeting', '2026-01-02T09:00:00Z',
  '2026-01-02T09:30:00Z', 'https://example.invalid/meeting',
  'actor-a', 'scheduled', '{"subject":"Review Meeting"}',
  '2026-01-02T09:00:00Z', '2026-01-02T12:00:00Z'
);
