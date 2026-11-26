"""Transactional state for one local OmniService instance."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
 id TEXT PRIMARY KEY, channel TEXT NOT NULL, account_id TEXT NOT NULL,
 chat_id TEXT NOT NULL, user_id TEXT NOT NULL, recipient_type TEXT NOT NULL,
 language TEXT NOT NULL DEFAULT 'und', language_at REAL NOT NULL DEFAULT 0,
 last_customer_at REAL NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'auto',
 room_id TEXT UNIQUE, room_status TEXT NOT NULL DEFAULT 'absent', created_at REAL NOT NULL,
 UNIQUE(channel, account_id, chat_id)
);
CREATE TABLE IF NOT EXISTS members (
 conversation_id TEXT NOT NULL REFERENCES conversations(id), user_id TEXT NOT NULL,
 PRIMARY KEY(conversation_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
 direction TEXT NOT NULL, source_channel TEXT NOT NULL, source_id TEXT NOT NULL,
 author_id TEXT NOT NULL, sender_name TEXT NOT NULL, original TEXT NOT NULL,
 translated TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT 'und',
 kind TEXT NOT NULL DEFAULT 'text', attachments TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL, external_id TEXT, parent_id TEXT, recall_status TEXT,
 created_at REAL NOT NULL,
 UNIQUE(conversation_id, source_channel, source_id)
);
CREATE TABLE IF NOT EXISTS inbox (
 channel TEXT NOT NULL, account_id TEXT NOT NULL, event_id TEXT NOT NULL,
 digest TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(channel, account_id, event_id)
);
CREATE TABLE IF NOT EXISTS tombstones (
 channel TEXT NOT NULL, account_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 source_message_id TEXT NOT NULL, deleted_at REAL NOT NULL,
 PRIMARY KEY(channel, account_id, chat_id, source_message_id)
);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_key TEXT UNIQUE NOT NULL,
 conversation_id TEXT NOT NULL REFERENCES conversations(id), message_id TEXT,
 channel TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 available_at REAL NOT NULL, last_error TEXT NOT NULL DEFAULT '', external_id TEXT
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
 conversation_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, available_at, id);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, created_at);
PRAGMA user_version = 1;
"""


class Store:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.execute('PRAGMA journal_mode = WAL')
        self.db.execute('PRAGMA busy_timeout = 5000')
        self.db.executescript(SCHEMA)
        if 'external_id' not in {row[1] for row in self.db.execute('PRAGMA table_info(outbox)')}:
            self.db.execute('ALTER TABLE outbox ADD COLUMN external_id TEXT')
        with self.transaction() as db:
            db.execute("UPDATE outbox SET status='unknown', last_error='process_interrupted' WHERE status='inflight'")
            db.execute("UPDATE messages SET status='delivery_unknown' WHERE id IN (SELECT message_id FROM outbox WHERE status='unknown' AND operation='send')")
            db.execute("UPDATE messages SET recall_status='delivery_unknown' WHERE id IN (SELECT message_id FROM outbox WHERE status='unknown' AND operation='delete')")
            db.execute("UPDATE conversations SET room_status='unknown' WHERE id IN (SELECT conversation_id FROM outbox WHERE status='unknown' AND operation='create_room')")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()
