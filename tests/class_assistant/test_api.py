import json
from types import SimpleNamespace

import pytest

from src.class_assistant.service import ClassAssistantService
from src.class_assistant.storage import Storage
from src.web import server


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


def call_api(service, path, command="GET", body=None):
    server.register_class_assistant_service(service)
    handler = object.__new__(server._UIHandler)
    handler.path = path
    handler.command = command
    handler._json_body = lambda: body or {}
    result = []
    handler.send_json = result.append
    handler._handle_class_assistant_request()
    return result[0]


def test_status_and_queue_api_never_exposes_model_config(tmp_path):
    service = ClassAssistantService(Config(), storage=Storage(str(tmp_path / "assistant.db")))
    response = call_api(service, "/api/class-assistant/status")
    assert response["ok"] is True
    assert response["status"]["dry_run"] is True
    assert "deepseek_api_key" not in json.dumps(response)
    groups = call_api(service, "/api/class-assistant/groups")
    assert groups["items"][0]["chat_id"] == "class@chatroom"


def test_send_api_requires_version_and_confirmation_token(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))
    service = ClassAssistantService(Config(), storage=storage)
    storage.insert_reply_draft({
        "id": "draft-1", "version": 1, "chat_id": "class@chatroom",
        "group_name": "Class", "text": "收到", "status": "pending_review",
        "risk_level": "low",
    })
    service.approve_draft("draft-1", 1)
    response = call_api(service, "/api/class-assistant/drafts/draft-1/send", "POST", {"version": 1})
    assert response["ok"] is False
    assert "confirmation_token" in response["error"]


def test_emergency_stop_blocks_future_collection(tmp_path):
    storage = Storage(str(tmp_path / "assistant.db"))
    service = ClassAssistantService(Config(), storage=storage)
    response = call_api(service, "/api/class-assistant/stop", "POST", {})
    assert response["ok"] is True
    service.handle({"message_id": "x", "chat_id": "class@chatroom", "is_group": True, "content": "通知", "timestamp": 1})
    assert storage.count_messages() == 0
