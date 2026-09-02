from datetime import datetime
from pathlib import Path

import pytest

from src.class_assistant.service import ClassAssistantService
from src.class_assistant.storage import Storage


class Config:
    class_assistant_enabled = True
    class_assistant_collection_enabled = True
    class_assistant_analysis_enabled = True
    class_assistant_real_send_enabled = False
    class_assistant_dry_run = True
    class_assistant_groups = ["class@chatroom"]
    db_path = "data/test-class-assistant.db"
    timezone = "Asia/Shanghai"
    digest_schedule = "08:00,20:00"
    raw_message_retention_days = 7
    draft_retention_days = 30


def message(message_id, chat_id="class@chatroom", is_group=True, timestamp=1):
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "group_name": "Class",
        "sender_id": "teacher",
        "sender_name": "老师",
        "content": "明天17点前提交实验报告",
        "msg_type": 1,
        "timestamp": timestamp,
        "is_group": is_group,
    }


def test_handle_collects_only_whitelisted_groups_and_never_returns_reply(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))
    service = ClassAssistantService(Config(), storage=storage)

    assert service.handle(message("ok")) is None
    assert service.handle(message("private", "user", False)) is None
    assert service.handle(message("other", "other@chatroom")) is None
    assert storage.count_messages() == 1


def test_run_digest_persists_todo_and_reply_draft_without_sending(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))
    calls = []

    def model(_messages):
        calls.append(True)
        return {
            "summary": "需要提交实验报告",
            "todos": [{"title": "提交实验报告", "due_at": "2026-09-03T17:00:00+08:00"}],
            "reply_candidates": [{"text": "老师您好，收到通知，我会按时提交。", "source_message_id": "m1"}],
        }

    service = ClassAssistantService(Config(), storage=storage, model_call=model)
    service.handle(message("m1", timestamp=1_756_800_000))
    result = service.run_digest(now=datetime.fromisoformat("2026-09-02T20:01:00+08:00"), force=True)

    assert calls == [True]
    assert result["status"] == "succeeded"
    assert len(storage.query("todo_items")) == 1
    drafts = storage.query("reply_drafts")
    assert len(drafts) == 1
    assert drafts[0]["status"] == "pending_review"
    service.approve_draft(drafts[0]["id"], version=1)
    assert service.send_draft(drafts[0]["id"], confirmation_token=None)["dry_run"] is True


def test_failed_digest_does_not_mark_success(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))

    def broken_model(_messages):
        raise RuntimeError("model unavailable")

    service = ClassAssistantService(Config(), storage=storage, model_call=broken_model)
    service.handle(message("m1", timestamp=1_756_800_000))
    result = service.run_digest(now=datetime.fromisoformat("2026-09-02T20:01:00+08:00"), force=True)

    assert result["status"] == "failed"
    assert not storage.query("digest_runs", status="succeeded")


def test_approval_requires_latest_version(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))
    service = ClassAssistantService(Config(), storage=storage)
    service.handle(message("m1", timestamp=1_756_800_000))
    service._storage.insert_reply_draft({
        "id": "draft-1", "version": 1, "chat_id": "class@chatroom",
        "text": "收到", "status": "pending_review", "risk_level": "low",
    })

    assert service.approve_draft("draft-1", version=1)["status"] == "approved"
    with pytest.raises(ValueError):
        service.approve_draft("draft-1", version=2)


def test_edit_creates_new_version_and_revokes_approval(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))
    service = ClassAssistantService(Config(), storage=storage)
    storage.insert_reply_draft({
        "id": "draft-1", "version": 1, "chat_id": "class@chatroom",
        "group_name": "Class", "text": "收到", "status": "pending_review",
        "risk_level": "low",
    })
    service.approve_draft("draft-1", version=1)
    edited = service.edit_draft("draft-1", "老师您好，收到。")
    assert edited["version"] == 2
    assert edited["status"] == "edited"
    with pytest.raises(ValueError):
        service.send_draft("draft-1", version=1)


def test_real_send_crash_leaves_sending_for_reconciliation(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))
    config = Config()
    config.class_assistant_dry_run = False
    config.class_assistant_real_send_enabled = True

    def broken_sender(_chat_id, _text):
        raise RuntimeError("window disappeared")

    service = ClassAssistantService(config, storage=storage, sender=broken_sender)
    storage.insert_reply_draft({
        "id": "draft-1", "version": 1, "chat_id": "class@chatroom",
        "group_name": "Class", "text": "收到", "status": "pending_review",
        "risk_level": "low",
    })
    service.approve_draft("draft-1", version=1)
    token = service.issue_confirmation_token()
    with pytest.raises(RuntimeError):
        service.send_draft("draft-1", version=1, confirmation_token=token)
    assert storage.query("reply_drafts", status="sending")


def test_failed_analysis_does_not_advance_analysis_cursor(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))
    config = Config()
    calls = []

    def model(messages):
        calls.append(messages)
        raise RuntimeError("temporary outage")

    service = ClassAssistantService(config, storage=storage, model_call=model)
    service.handle(message("m1", timestamp=100))
    service.run_digest(now=datetime.fromisoformat("2026-09-02T20:01:00+08:00"), force=True)
    assert storage.get_analysis_cursor("class@chatroom") == (0, "")
    assert len(calls) == 1


def test_storage_persistent_fingerprint_deduplicates_restart(tmp_path):
    path = str(tmp_path / "assistant.db")
    first = Storage(path)
    first.insert_message(message("one", timestamp=100))
    first.close()
    second = Storage(path)
    second.insert_message(message("different-id", timestamp=100))
    assert second.count_messages() == 1


def test_high_risk_draft_requires_edit_before_approval(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))
    service = ClassAssistantService(Config(), storage=storage)
    storage.insert_reply_draft({
        "id": "draft-risk", "version": 1, "chat_id": "class@chatroom",
        "group_name": "Class", "text": "我申请请假", "status": "pending_review",
        "risk_level": "high",
    })
    with pytest.raises(ValueError):
        service.approve_draft("draft-risk", 1)
    edited = service.edit_draft("draft-risk", "老师您好，我想申请请假，请您确认。")
    assert service.approve_draft("draft-risk", edited["version"])["status"] == "approved"
