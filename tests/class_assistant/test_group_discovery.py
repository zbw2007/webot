import pytest

from src.class_assistant.group_discovery import discover_groups


class FakeClient:
    def __init__(self):
        self.messages_called = False

    def get_sessions(self):
        return [
            {"username": "z@chatroom", "nickname": "Z 群"},
            {"username": "alice", "nickname": "Alice"},
            {"username": "a@chatroom", "nickname": "A 群"},
        ]

    def get_group_members(self, chat_id):
        return {"z@chatroom": [{"wxid": "secret-1"}], "a@chatroom": [{"wxid": "secret-2"}, {"wxid": "secret-3"}]}[chat_id]

    def get_messages(self, **_kwargs):
        self.messages_called = True
        raise AssertionError("group discovery must not read messages")


def test_discover_groups_returns_sorted_metadata_only():
    client = FakeClient()
    assert discover_groups(client) == [
        {"chat_id": "a@chatroom", "display_name": "A 群", "member_count": 2},
        {"chat_id": "z@chatroom", "display_name": "Z 群", "member_count": 1},
    ]
    assert not client.messages_called


def test_discover_groups_converts_backend_errors_to_controlled_error():
    class Broken(FakeClient):
        def get_sessions(self):
            raise RuntimeError("backend unavailable")

    with pytest.raises(RuntimeError, match="^group discovery unavailable$") as error:
        discover_groups(Broken())
    assert "backend unavailable" not in str(error.value)
    assert "C:\\secret" not in str(error.value)


def test_discover_groups_keeps_group_when_member_lookup_fails_and_deduplicates():
    class PartialClient(FakeClient):
        def get_sessions(self):
            return [
                {"username": "broken@chatroom", "nickname": "Broken"},
                {"username": "ok@chatroom", "nickname": "OK"},
                {"username": "ok@chatroom", "nickname": "Duplicate"},
            ]

        def get_group_members(self, chat_id):
            if chat_id == "broken@chatroom":
                raise RuntimeError("members unavailable")
            return [{"wxid": "member"}]

    result = discover_groups(PartialClient())
    assert result == [
        {"chat_id": "broken@chatroom", "display_name": "Broken", "member_count": 0},
        {"chat_id": "ok@chatroom", "display_name": "OK", "member_count": 1},
    ]
