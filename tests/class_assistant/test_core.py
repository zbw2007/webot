import sqlite3
from datetime import datetime, timezone

import pytest

from src.class_assistant.whitelist import GroupWhitelist
from src.class_assistant.dedup import message_fingerprint, Deduplicator
from src.class_assistant.scheduler import scheduled_slot, catch_up_slot
from src.class_assistant.state_machine import transition
from src.class_assistant.send_guard import SendGuard, SendBlocked
from src.class_assistant.storage import Storage
from src.class_assistant.collector import ReadOnlyCollector


def test_whitelist_requires_explicit_group_and_rejects_private():
    wl = GroupWhitelist(["room@chatroom"])
    assert wl.allows("room@chatroom", True)
    assert not wl.allows("someone", False)
    with pytest.raises(ValueError):
        GroupWhitelist(["*"])
    with pytest.raises(ValueError):
        GroupWhitelist([""])


def test_dedup_uses_id_and_content_fingerprint():
    d = Deduplicator()
    m = {"chat_id": "g", "message_id": "1", "content": "通知", "timestamp": 10}
    assert d.accept(m)
    assert not d.accept(dict(m))
    assert message_fingerprint(m) == message_fingerprint(dict(m))


def test_schedule_and_catch_up_once():
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    assert scheduled_slot(datetime(2026, 9, 2, 8, 1, tzinfo=timezone.utc), "UTC") == "2026-09-02T08:00+00:00"
    assert catch_up_slot(now, {"2026-09-02T08:00+08:00"}, "Asia/Shanghai") is None
    assert catch_up_slot(now, set(), "Asia/Shanghai") == "2026-09-02T08:00+08:00"


def test_state_machine_rejects_invalid_transition():
    assert transition("pending_review", "approve") == "approved"
    assert transition("approved", "send") == "sending"
    with pytest.raises(ValueError):
        transition("pending_review", "send")


def test_send_guard_requires_approval_and_dry_run():
    guard = SendGuard(real_send_enabled=False, dry_run=True)
    draft = {"status": "approved", "version": 2, "approved_version": 2, "chat_id": "g", "send_fingerprint": "f"}
    assert guard.check(draft, "g", {"g"}, set()) is True
    with pytest.raises(SendBlocked):
        guard.check({**draft, "status": "pending_review"}, "g", {"g"}, set())
    with pytest.raises(SendBlocked):
        guard.check(draft, "other", {"g"}, set())


def test_storage_retention_and_collector_cursor(tmp_path):
    s = Storage(str(tmp_path / "db.sqlite"))
    s.insert_message({"message_id": "1", "chat_id": "g", "group_name": "G", "sender_id": "u", "sender_name": "U", "content": "x", "msg_type": 1, "timestamp": 1})
    s.insert_message({"message_id": "old", "chat_id": "g", "group_name": "G", "sender_id": "u", "sender_name": "U", "content": "old", "msg_type": 1, "timestamp": 1}, expires_at=1)
    assert s.count_messages() == 2
    assert s.cleanup(now=2, raw_days=7) == 1
    rows = [{"message_id": str(i), "chat_id": "g", "group_name": "G", "sender_id": "u", "sender_name": "U", "content": str(i), "msg_type": 1, "timestamp": i} for i in range(1, 4)]
    calls = []
    def fetch(cursor, limit):
        calls.append(cursor)
        return [r for r in rows if (r["timestamp"], r["message_id"]) > tuple(cursor)][:limit]
    c = ReadOnlyCollector(fetch, s, GroupWhitelist(["g"]), page_size=2)
    assert c.poll() == (3, "3")
    assert c.cursor == (3, "3")
    assert calls == [(0, ""), (2, "2"), (3, "3")]


def test_collector_compound_cursor_does_not_skip_same_timestamp(tmp_path):
    s = Storage(str(tmp_path / "db.sqlite"))
    rows = [
        {"message_id": "a", "chat_id": "private", "is_group": False, "content": "x", "timestamp": 10},
        {"message_id": "b", "chat_id": "g", "is_group": True, "content": "y", "timestamp": 10},
    ]
    calls = []
    def fetch(cursor, limit):
        calls.append(cursor)
        return [r for r in rows if (r["timestamp"], r["message_id"]) > tuple(cursor)][:limit]
    c = ReadOnlyCollector(fetch, s, GroupWhitelist(["g"]), page_size=1)
    c.poll()
    assert c.cursor == (10, "b")
    assert s.count_messages() == 1
    assert calls[0] == (0, "")
