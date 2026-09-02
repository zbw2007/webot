import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def _locked(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


class Storage:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
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
        CREATE TABLE IF NOT EXISTS digest_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, scheduled_slot TEXT UNIQUE NOT NULL, status TEXT NOT NULL, is_catch_up INTEGER NOT NULL DEFAULT 0, window_start TEXT, window_end TEXT, started_at INTEGER, completed_at INTEGER, error TEXT);
        CREATE TABLE IF NOT EXISTS todo_items (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, due_at TEXT, due_confidence TEXT NOT NULL DEFAULT 'unknown', status TEXT NOT NULL DEFAULT 'open', source_message_id TEXT, created_at INTEGER NOT NULL DEFAULT 0, completed_at INTEGER);
        CREATE TABLE IF NOT EXISTS reply_drafts (id TEXT NOT NULL, version INTEGER NOT NULL, chat_id TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, status TEXT NOT NULL, risk_level TEXT NOT NULL DEFAULT 'low', approved_version INTEGER, send_fingerprint TEXT, source_message_id TEXT, created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER, UNIQUE(id, version));
        CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT, action TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS group_whitelist (chat_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS analysis_cursors (chat_id TEXT PRIMARY KEY, timestamp INTEGER NOT NULL DEFAULT 0, message_id TEXT NOT NULL DEFAULT '');
        """)
        # Add fields when opening databases created by the initial safety-core release.
        for table, columns in {
            "digest_runs": {"run_id": "TEXT", "window_start": "TEXT", "window_end": "TEXT", "started_at": "INTEGER", "completed_at": "INTEGER", "error": "TEXT"},
            "todo_items": {"group_id": "TEXT NOT NULL DEFAULT ''", "due_confidence": "TEXT NOT NULL DEFAULT 'unknown'", "created_at": "INTEGER NOT NULL DEFAULT 0", "completed_at": "INTEGER"},
            "reply_drafts": {"group_name": "TEXT NOT NULL DEFAULT ''", "approved_version": "INTEGER", "send_fingerprint": "TEXT", "source_message_id": "TEXT", "created_at": "INTEGER NOT NULL DEFAULT 0", "expires_at": "INTEGER"},
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

    @_locked
    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        """Run a group of writes atomically on this connection.

        The individual write helpers accept ``commit=False`` so a digest can
        validate and persist all groups as one unit.  The re-entrant lock also
        keeps concurrent scheduler/API operations from interleaving writes.
        """
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    @_locked
    def insert_message(self, message, expires_at=None):
        if not message.get("content_fingerprint"):
            from .dedup import message_fingerprint
            message = dict(message)
            message["content_fingerprint"] = message_fingerprint(message)
        expires_at = int(time.time()) + 7 * 86400 if expires_at is None else expires_at
        duplicate = self.conn.execute(
            "SELECT * FROM captured_messages WHERE chat_id=? AND (message_id=? OR (content_fingerprint IS NOT NULL AND content_fingerprint=?))",
            (message["chat_id"], message["message_id"], message["content_fingerprint"]),
        ).fetchone()
        if duplicate is not None:
            return duplicate
        self.conn.execute("INSERT OR IGNORE INTO captured_messages(message_id,chat_id,group_name,sender_id,sender_name,content,msg_type,timestamp,content_fingerprint,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (message["message_id"], message["chat_id"], message.get("group_name", ""), message.get("sender_id", ""), message.get("sender_name", ""), message.get("content", ""), message.get("msg_type", 1), message["timestamp"], message.get("content_fingerprint"), expires_at))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM captured_messages WHERE chat_id=? AND message_id=?", (message["chat_id"], message["message_id"])).fetchone()

    @_locked
    def get_analysis_cursor(self, chat_id):
        row = self.conn.execute("SELECT timestamp, message_id FROM analysis_cursors WHERE chat_id=?", (chat_id,)).fetchone()
        return (int(row[0]), str(row[1])) if row else (0, "")

    @_locked
    def advance_analysis_cursor(self, chat_id, timestamp, message_id, *, commit=True):
        current = self.get_analysis_cursor(chat_id)
        position = (int(timestamp), str(message_id))
        if position <= current:
            return current
        self.conn.execute(
            "INSERT INTO analysis_cursors(chat_id,timestamp,message_id) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET timestamp=excluded.timestamp,message_id=excluded.message_id",
            (chat_id, position[0], position[1]),
        )
        if commit:
            self.conn.commit()
        return position

    @_locked
    def count_messages(self):
        return self.conn.execute("SELECT COUNT(*) FROM captured_messages").fetchone()[0]

    @_locked
    def cleanup(self, now=None, raw_days=7, draft_days=30, audit_days=30):
        now = int(time.time()) if now is None else now
        cutoff = now - raw_days * 86400
        cur = self.conn.execute("DELETE FROM captured_messages WHERE expires_at <= ? OR timestamp <= ?", (now, cutoff))
        draft_cutoff = now - draft_days * 86400
        draft_cur = self.conn.execute("DELETE FROM reply_drafts WHERE (expires_at IS NOT NULL AND expires_at <= ?) OR (created_at > 0 AND created_at <= ?)", (now, draft_cutoff))
        audit_cutoff = now - audit_days * 86400
        self.conn.execute("DELETE FROM audit_events WHERE created_at <= ?", (audit_cutoff,))
        self.conn.commit()
        return cur.rowcount

    @_locked
    def insert_digest_run(self, run, *, commit=True):
        self.conn.execute("INSERT OR REPLACE INTO digest_runs(run_id,scheduled_slot,status,is_catch_up,window_start,window_end,started_at,completed_at,error) VALUES(?,?,?,?,?,?,?,?,?)", tuple(run.get(k) for k in ("run_id", "scheduled_slot", "status", "is_catch_up", "window_start", "window_end", "started_at", "completed_at", "error")))
        if commit:
            self.conn.commit()
        return self.conn.execute("SELECT * FROM digest_runs WHERE scheduled_slot=?", (run["scheduled_slot"],)).fetchone()

    @_locked
    def insert_todo(self, todo, *, commit=True):
        existing = self.conn.execute(
            "SELECT * FROM todo_items WHERE group_id=? AND title=? "
            "AND COALESCE(due_at,'')=COALESCE(?, '') "
            "AND COALESCE(source_message_id,'')=COALESCE(?, '') LIMIT 1",
            (todo.get("group_id", ""), todo["title"], todo.get("due_at"), todo.get("source_message_id")),
        ).fetchone()
        if existing is not None:
            return existing
        cur = self.conn.execute("INSERT INTO todo_items(group_id,title,due_at,due_confidence,status,source_message_id,created_at) VALUES(?,?,?,?,?,?,?)", (todo.get("group_id", ""), todo["title"], todo.get("due_at"), todo.get("due_confidence", "unknown"), todo.get("status", "open"), todo.get("source_message_id"), todo.get("created_at", int(time.time()))))
        if commit:
            self.conn.commit()
        return self.conn.execute("SELECT * FROM todo_items WHERE id=?", (cur.lastrowid,)).fetchone()

    @_locked
    def insert_reply_draft(self, draft, *, commit=True):
        self.conn.execute("INSERT OR IGNORE INTO reply_drafts(id,version,chat_id,group_name,text,status,risk_level,approved_version,send_fingerprint,source_message_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (draft["id"], draft["version"], draft["chat_id"], draft.get("group_name", ""), draft["text"], draft.get("status", "pending_review"), draft.get("risk_level", "low"), draft.get("approved_version"), draft.get("send_fingerprint"), draft.get("source_message_id"), draft.get("created_at", int(time.time())), draft.get("expires_at")))
        if commit:
            self.conn.commit()
        return self.conn.execute("SELECT * FROM reply_drafts WHERE id=? AND version=?", (draft["id"], draft["version"])).fetchone()

    @_locked
    def update_draft_status(self, draft_id, version, status, approved_version=None):
        self.conn.execute(
            "UPDATE reply_drafts SET status=?, approved_version=? WHERE id=? AND version=?",
            (status, approved_version, draft_id, version),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM reply_drafts WHERE id=? AND version=?", (draft_id, version)
        ).fetchone()
        return dict(row) if row else None

    @_locked
    def claim_draft_for_sending(self, draft_id, version):
        """Atomically claim an approved draft for a real send attempt."""
        cur = self.conn.execute(
            "UPDATE reply_drafts SET status='sending' "
            "WHERE id=? AND version=? AND status='approved' AND approved_version=?",
            (draft_id, version, version),
        )
        self.conn.commit()
        return cur.rowcount == 1

    @_locked
    def set_draft_fingerprint(self, draft_id, version, fingerprint):
        self.conn.execute(
            "UPDATE reply_drafts SET send_fingerprint=? WHERE id=? AND version=?",
            (fingerprint, draft_id, version),
        )
        self.conn.commit()

    @_locked
    def insert_audit(self, event):
        cur = self.conn.execute("INSERT INTO audit_events(draft_id,action,actor,details,created_at) VALUES(?,?,?,?,?)", (event.get("draft_id"), event["action"], event.get("actor", ""), event.get("details", ""), event.get("created_at", int(time.time()))))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM audit_events WHERE id=?", (cur.lastrowid,)).fetchone()

    @_locked
    def query(self, table, **filters):
        if table not in {"digest_runs", "todo_items", "reply_drafts", "audit_events", "captured_messages", "group_whitelist"}:
            raise ValueError("invalid storage table")
        allowed_columns = {
            "digest_runs": {"run_id", "scheduled_slot", "status", "is_catch_up"},
            "todo_items": {"id", "group_id", "status"},
            "reply_drafts": {"id", "version", "chat_id", "status"},
            "audit_events": {"id", "draft_id", "action"},
            "captured_messages": {"id", "message_id", "chat_id", "timestamp"},
            "group_whitelist": {"chat_id", "enabled"},
        }[table]
        if any(key not in allowed_columns for key in filters):
            raise ValueError("invalid storage filter")
        clause = " AND ".join(f"{k} = ?" for k in filters) if filters else "1=1"
        return self.conn.execute(f"SELECT * FROM {table} WHERE {clause}", tuple(filters.values())).fetchall()
