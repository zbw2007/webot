import sqlite3
import time
from pathlib import Path


class Storage:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS captured_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL,
            chat_id TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '',
            sender_id TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '', msg_type INTEGER NOT NULL DEFAULT 1,
            timestamp INTEGER NOT NULL, content_fingerprint TEXT, expires_at INTEGER NOT NULL,
            UNIQUE(chat_id, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_captured_chat_time ON captured_messages(chat_id, timestamp);
        CREATE TABLE IF NOT EXISTS digest_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, scheduled_slot TEXT UNIQUE NOT NULL, status TEXT NOT NULL, is_catch_up INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS todo_items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, due_at TEXT, status TEXT NOT NULL DEFAULT 'open', source_message_id TEXT);
        CREATE TABLE IF NOT EXISTS reply_drafts (id TEXT NOT NULL, version INTEGER NOT NULL, chat_id TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, risk_level TEXT NOT NULL DEFAULT 'low', UNIQUE(id, version));
        CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT, action TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS group_whitelist (chat_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1);
        """)
        self.conn.commit()

    def insert_message(self, message, expires_at=None):
        expires_at = int(time.time()) + 7 * 86400 if expires_at is None else expires_at
        self.conn.execute("INSERT OR IGNORE INTO captured_messages(message_id,chat_id,group_name,sender_id,sender_name,content,msg_type,timestamp,content_fingerprint,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (message["message_id"], message["chat_id"], message.get("group_name", ""), message.get("sender_id", ""), message.get("sender_name", ""), message.get("content", ""), message.get("msg_type", 1), message["timestamp"], message.get("content_fingerprint"), expires_at))
        self.conn.commit()

    def count_messages(self):
        return self.conn.execute("SELECT COUNT(*) FROM captured_messages").fetchone()[0]

    def cleanup(self, now=None, raw_days=7, draft_days=30):
        now = int(time.time()) if now is None else now
        cutoff = now - raw_days * 86400
        cur = self.conn.execute("DELETE FROM captured_messages WHERE expires_at <= ? OR timestamp <= ?", (now, cutoff))
        self.conn.commit()
        return cur.rowcount

