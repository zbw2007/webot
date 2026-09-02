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
        CREATE TABLE IF NOT EXISTS digest_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, scheduled_slot TEXT UNIQUE NOT NULL, status TEXT NOT NULL, is_catch_up INTEGER NOT NULL DEFAULT 0, started_at INTEGER, completed_at INTEGER, error TEXT);
        CREATE TABLE IF NOT EXISTS todo_items (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, due_at TEXT, status TEXT NOT NULL DEFAULT 'open', source_message_id TEXT, created_at INTEGER NOT NULL DEFAULT 0, completed_at INTEGER);
        CREATE TABLE IF NOT EXISTS reply_drafts (id TEXT NOT NULL, version INTEGER NOT NULL, chat_id TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, status TEXT NOT NULL, risk_level TEXT NOT NULL DEFAULT 'low', approved_version INTEGER, send_fingerprint TEXT, created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER, UNIQUE(id, version));
        CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT, action TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS group_whitelist (chat_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1);
        """)
        # Add fields when opening databases created by the initial safety-core release.
        for table, columns in {
            "digest_runs": {"run_id": "TEXT", "started_at": "INTEGER", "completed_at": "INTEGER", "error": "TEXT"},
            "todo_items": {"group_id": "TEXT NOT NULL DEFAULT ''", "created_at": "INTEGER NOT NULL DEFAULT 0", "completed_at": "INTEGER"},
            "reply_drafts": {"group_name": "TEXT NOT NULL DEFAULT ''", "approved_version": "INTEGER", "send_fingerprint": "TEXT", "created_at": "INTEGER NOT NULL DEFAULT 0", "expires_at": "INTEGER"},
            "audit_events": {"actor": "TEXT NOT NULL DEFAULT ''", "details": "TEXT NOT NULL DEFAULT ''"},
        }.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        self.conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_digest_runs_slot ON digest_runs(scheduled_slot);
        CREATE INDEX IF NOT EXISTS idx_todo_group_status ON todo_items(group_id, status);
        CREATE INDEX IF NOT EXISTS idx_reply_drafts_chat_status ON reply_drafts(chat_id, status);
        CREATE INDEX IF NOT EXISTS idx_audit_draft_time ON audit_events(draft_id, created_at);
        """)
        self.conn.commit()

    def insert_message(self, message, expires_at=None):
        expires_at = int(time.time()) + 7 * 86400 if expires_at is None else expires_at
        self.conn.execute("INSERT OR IGNORE INTO captured_messages(message_id,chat_id,group_name,sender_id,sender_name,content,msg_type,timestamp,content_fingerprint,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (message["message_id"], message["chat_id"], message.get("group_name", ""), message.get("sender_id", ""), message.get("sender_name", ""), message.get("content", ""), message.get("msg_type", 1), message["timestamp"], message.get("content_fingerprint"), expires_at))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM captured_messages WHERE chat_id=? AND message_id=?", (message["chat_id"], message["message_id"])).fetchone()

    def count_messages(self):
        return self.conn.execute("SELECT COUNT(*) FROM captured_messages").fetchone()[0]

    def cleanup(self, now=None, raw_days=7, draft_days=30):
        now = int(time.time()) if now is None else now
        cutoff = now - raw_days * 86400
        cur = self.conn.execute("DELETE FROM captured_messages WHERE expires_at <= ? OR timestamp <= ?", (now, cutoff))
        draft_cutoff = now - draft_days * 86400
        draft_cur = self.conn.execute("DELETE FROM reply_drafts WHERE expires_at IS NOT NULL AND expires_at <= ? OR created_at > 0 AND created_at <= ?", (now, draft_cutoff))
        self.conn.commit()
        return cur.rowcount

    def insert_digest_run(self, run):
        self.conn.execute("INSERT OR REPLACE INTO digest_runs(run_id,scheduled_slot,status,is_catch_up,started_at,completed_at,error) VALUES(?,?,?,?,?,?,?)", tuple(run.get(k) for k in ("run_id", "scheduled_slot", "status", "is_catch_up")) + (run.get("started_at"), run.get("completed_at"), run.get("error")))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM digest_runs WHERE scheduled_slot=?", (run["scheduled_slot"],)).fetchone()

    def insert_todo(self, todo):
        cur = self.conn.execute("INSERT INTO todo_items(group_id,title,due_at,status,source_message_id,created_at) VALUES(?,?,?,?,?,?)", (todo.get("group_id", ""), todo["title"], todo.get("due_at"), todo.get("status", "open"), todo.get("source_message_id"), todo.get("created_at", int(time.time()))))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM todo_items WHERE id=?", (cur.lastrowid,)).fetchone()

    def insert_reply_draft(self, draft):
        self.conn.execute("INSERT OR REPLACE INTO reply_drafts(id,version,chat_id,group_name,text,status,risk_level,approved_version,send_fingerprint,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (draft["id"], draft["version"], draft["chat_id"], draft.get("group_name", ""), draft["text"], draft.get("status", "pending_review"), draft.get("risk_level", "low"), draft.get("approved_version"), draft.get("send_fingerprint"), draft.get("created_at", int(time.time())), draft.get("expires_at")))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM reply_drafts WHERE id=? AND version=?", (draft["id"], draft["version"])).fetchone()

    def insert_audit(self, event):
        cur = self.conn.execute("INSERT INTO audit_events(draft_id,action,actor,details,created_at) VALUES(?,?,?,?,?)", (event.get("draft_id"), event["action"], event.get("actor", ""), event.get("details", ""), event.get("created_at", int(time.time()))))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM audit_events WHERE id=?", (cur.lastrowid,)).fetchone()

    def query(self, table, **filters):
        if table not in {"digest_runs", "todo_items", "reply_drafts", "audit_events", "captured_messages"}:
            raise ValueError("invalid storage table")
        clause = " AND ".join(f"{k} = ?" for k in filters) if filters else "1=1"
        return self.conn.execute(f"SELECT * FROM {table} WHERE {clause}", tuple(filters.values())).fetchall()
