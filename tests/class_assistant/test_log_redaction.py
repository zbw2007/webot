import logging
from datetime import datetime

from src.class_assistant.service import ClassAssistantService
from src.class_assistant.storage import Storage
from src.wechat.wcdb_backend import WcdbBackend


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
    audit_retention_days = 30


def _message():
    return {
        "message_id": "m1", "chat_id": "class@chatroom", "group_name": "Class",
        "sender_id": "teacher", "sender_name": "老师", "content": "通知",
        "msg_type": 1, "timestamp": 1_756_800_000, "is_group": True,
    }


def test_digest_failure_does_not_expose_exception_in_result_db_or_logs(tmp_path, caplog):
    secret = r"C:\Users\secret\prompt.txt"
    storage = Storage(str(tmp_path / "assistant.db"))
    service = ClassAssistantService(Config(), storage=storage,
                                   model_call=lambda _messages: (_ for _ in ()).throw(
                                       RuntimeError(f"provider failed: {secret}")))
    service.handle(_message())
    with caplog.at_level(logging.ERROR):
        result = service.run_digest(now=datetime.fromisoformat("2026-09-02T20:01:00+08:00"), force=True)
    assert secret not in repr(result)
    assert secret not in service.status()["last_error"]
    assert secret not in repr(storage.query("digest_runs"))
    assert secret not in caplog.text


def test_send_failure_audit_does_not_expose_exception(tmp_path):
    secret = r"C:\Users\secret\wechat.db"
    storage = Storage(str(tmp_path / "assistant.db"))
    config = Config()
    config.class_assistant_dry_run = False
    config.class_assistant_real_send_enabled = True
    service = ClassAssistantService(config, storage=storage,
        sender=lambda _chat, _text: (_ for _ in ()).throw(RuntimeError(secret)),
        window_validator=lambda _chat, _group: True)
    storage.insert_reply_draft({"id": "d", "version": 1, "chat_id": "class@chatroom",
        "group_name": "Class", "text": "收到", "status": "pending_review", "risk_level": "low"})
    service.approve_draft("d", 1)
    token = service.issue_confirmation_token()
    try:
        service.send_draft("d", version=1, confirmation_token=token)
    except RuntimeError:
        pass
    assert secret not in repr(storage.query("audit_events"))


def test_wcdb_persistence_failures_do_not_log_paths_or_exception_text(tmp_path, monkeypatch, caplog):
    secret = r"C:\Users\secret\group-members.json"
    monkeypatch.chdir(tmp_path)

    class BadPath:
        def write_text(self, *_args, **_kwargs):
            raise OSError(secret)
        def with_suffix(self, _suffix):
            return self
        def parent(self):
            return self

    # Force the atomic replacement path to fail without exposing its details.
    class BadFile:
        def write_text(self, *_args, **_kwargs):
            raise OSError(secret)

    original = WcdbBackend._save_group_members
    monkeypatch.setattr("pathlib.Path.write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(secret)))
    with caplog.at_level(logging.WARNING):
        original({"id@chatroom": {"wxid": "同学"}})
    assert secret not in caplog.text
