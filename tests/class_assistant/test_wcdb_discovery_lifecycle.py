import threading
import time
import importlib
import pytest
from types import SimpleNamespace

from src.wechat.wcdb_backend import WcdbBackend

wcdb_backend = importlib.import_module("src.wechat.wcdb_backend")


class LifecycleClient:
    def __init__(self, closed):
        self.closed = closed
        self.close_started = threading.Event()
        self.release_close = threading.Event()
        self.invalid_calls_during_close = []

    def init(self):
        pass

    def open(self):
        pass

    def close(self):
        self.close_started.set()
        self.closed.set()
        self.release_close.wait(timeout=2)
        self.closed.clear()

    def get_sessions(self):
        if self.closed.is_set():
            self.invalid_calls_during_close.append("get_sessions")
        return []

    def get_group_members(self, _chat_id):
        if self.closed.is_set():
            self.invalid_calls_during_close.append("get_group_members")
        return []

    def resolve_nickname(self, _username):
        if self.closed.is_set():
            self.invalid_calls_during_close.append("resolve_nickname")
        return "resolved"


class CleanupClient:
    def __init__(self):
        self.close_calls = 0

    def init(self):
        pass

    def open(self):
        pass

    def close(self):
        self.close_calls += 1

    def get_sessions(self):
        return []


class SessionClient:
    def __init__(self, sessions):
        self.sessions = sessions
        self.close_calls = 0

    def get_sessions(self):
        return self.sessions

    def init(self):
        pass

    def open(self):
        pass

    def get_group_members(self, _chat_id):
        return []

    def resolve_nickname(self, username):
        return username

    def close(self):
        self.close_calls += 1


class FailingLifecycleClient(SessionClient):
    def __init__(self, failure):
        super().__init__([])
        self.failure = failure

    def init(self):
        if self.failure == "init":
            raise RuntimeError("init failed")

    def open(self):
        if self.failure == "open":
            raise RuntimeError("open failed")

    def get_sessions(self):
        if self.failure == "resolve":
            raise RuntimeError("resolve failed")
        return super().get_sessions()


class ExplodingGroupName(str):
    def casefold(self):
        raise RuntimeError("group name resolution failed")


def test_reinitialize_serializes_close_and_discovery_client_calls(monkeypatch):
    closed = threading.Event()
    old_client = LifecycleClient(closed)
    new_client = LifecycleClient(threading.Event())
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._client = old_client
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: new_client)
    monkeypatch.setattr(backend._window, "find_hwnd", lambda: None)

    reinit = threading.Thread(target=backend._reinitialize)
    reinit.start()
    assert old_client.close_started.wait(timeout=2)
    discovery = threading.Thread(target=backend.discover_group_metadata)
    discovery.start()
    time.sleep(0.05)
    assert discovery.is_alive()
    old_client.release_close.set()
    reinit.join(timeout=2)
    discovery.join(timeout=2)
    assert not reinit.is_alive()
    assert not discovery.is_alive()
    assert old_client.invalid_calls_during_close == []


def test_start_closes_client_when_no_configured_groups_resolve(monkeypatch):
    client = CleanupClient()
    backend = WcdbBackend(groups=["missing@chatroom"])
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: client)

    backend.start(lambda _message: None)

    assert client.close_calls == 1
    assert backend._client is None


def test_reinitialize_clears_stale_mapping_when_new_client_has_no_match(monkeypatch):
    backend = WcdbBackend(groups=["missing@chatroom"])
    old_client = SessionClient([])
    new_client = SessionClient([])
    backend._client = old_client
    backend._talker_ids = {"old group": "old@chatroom"}
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: new_client)
    monkeypatch.setattr(backend._window, "find_hwnd", lambda: None)

    backend._reinitialize()

    assert backend._talker_ids == {}
    assert backend._talker_to_name("old@chatroom") == ""


def test_resolve_groups_discards_partial_mapping_when_resolution_raises():
    client = SessionClient([
        {"username": "first@chatroom", "displayName": "First"},
        {"username": "second@chatroom", "displayName": "Second"},
    ])
    backend = WcdbBackend(groups=["First", ExplodingGroupName("Second")])
    backend._client = client

    try:
        backend._resolve_groups()
    except RuntimeError as exc:
        assert str(exc) == "group name resolution failed"
    else:
        raise AssertionError("expected group resolution to fail")

    assert backend._talker_ids == {}


def test_stop_closes_and_clears_client():
    client = SessionClient([])
    backend = WcdbBackend(groups=["group@chatroom"])
    backend._client = client
    backend._running = True

    backend.stop()

    assert backend._running is False
    assert client.close_calls == 1
    assert backend._client is None


def test_stop_clears_talker_mapping_and_blocks_stale_send():
    client = SessionClient([])
    backend = WcdbBackend(groups=["group@chatroom"])
    backend._client = client
    backend._talker_ids = {"group@chatroom": "old@chatroom"}
    backend._running = True

    backend.stop()

    assert backend._client is None
    assert backend._talker_ids == {}
    assert backend.send_text("old@chatroom", "should not send") is False


@pytest.mark.parametrize("failure", ["init", "open", "resolve"])
def test_reinitialize_failure_clears_client_and_talker_mapping(monkeypatch, failure):
    backend = WcdbBackend(groups=["missing@chatroom"])
    backend._client = SessionClient([])
    backend._talker_ids = {"old group": "old@chatroom"}
    new_client = FailingLifecycleClient(failure)
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: new_client)

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        backend._reinitialize()

    assert backend._client is None
    assert backend._talker_ids == {}


def test_standardize_serializes_nickname_resolution_with_reinitialize(monkeypatch):
    closed = threading.Event()
    old_client = LifecycleClient(closed)
    new_client = LifecycleClient(threading.Event())
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._client = old_client
    backend._running = True
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: new_client)
    monkeypatch.setattr(backend._window, "find_hwnd", lambda: None)

    reinit = threading.Thread(target=backend._reinitialize)
    reinit.start()
    assert old_client.close_started.wait(timeout=2)
    standardize = threading.Thread(target=lambda: backend._standardize(
        {"sender_username": "wxid_sender", "content": "hello", "timestamp": 1},
        "class@chatroom", "class@chatroom",
    ))
    standardize.start()
    old_client.release_close.set()
    reinit.join(timeout=2)
    standardize.join(timeout=2)
    assert not reinit.is_alive()
    assert not standardize.is_alive()
    assert old_client.invalid_calls_during_close == []


class WhitelistClient(SessionClient):
    def __init__(self):
        super().__init__([
            {"username": "allowed@chatroom", "displayName": "Allowed"},
            {"username": "other@chatroom", "displayName": "Other"},
        ])
        self.member_calls = []
        self.name_calls = []

    def get_group_members(self, chat_id):
        self.member_calls.append(chat_id)
        return [{"username": "wxid_member"}]

    def get_display_names(self, wxids):
        self.name_calls.append(list(wxids))
        return {wxid: "Member" for wxid in wxids}


def test_class_assistant_resolves_and_persists_only_explicit_whitelist(monkeypatch, tmp_path):
    client = WhitelistClient()
    config = SimpleNamespace(class_assistant_enabled=True)
    backend = WcdbBackend(groups=["allowed@chatroom"], config=config)
    backend._client = client
    saved_names = []
    saved_members = []
    monkeypatch.setattr(wcdb_backend.WcdbBackend, "_save_group_names",
                        staticmethod(lambda data: saved_names.append(data)))
    monkeypatch.setattr(wcdb_backend.WcdbBackend, "_save_group_members",
                        staticmethod(lambda data: saved_members.append(data)))

    backend._resolve_groups()

    assert client.member_calls == ["allowed@chatroom", "allowed@chatroom"]
    assert client.name_calls == [["wxid_member"]]
    assert saved_names == [{"allowed@chatroom": {"name": "Allowed", "member_count": 1}}]
    assert saved_members == [{"allowed@chatroom": {"wxid_member": "Member"}}]
    assert backend._talker_ids == {
        "allowed@chatroom": "allowed@chatroom",
        "Allowed": "allowed@chatroom",
    }


class PagingClient:
    def __init__(self, messages):
        self.messages = messages
        self.offsets = []

    def get_messages(self, talker, limit=50, offset=0):
        self.offsets.append((talker, limit, offset))
        newest_first = list(reversed(self.messages))
        return newest_first[offset:offset + limit]

    def resolve_nickname(self, sender):
        return sender


def test_poll_group_reads_all_pages_and_deduplicates_on_next_poll():
    messages = [
        {"sender_username": "wxid_sender", "content": f"message {i}",
         "timestamp": i, "server_id": str(i)}
        for i in range(120)
    ]
    client = PagingClient(messages)
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._client = client
    backend._running = True
    received = []

    backend._poll_group("class@chatroom", "class@chatroom", received.append)
    backend._poll_group("class@chatroom", "class@chatroom", received.append)

    assert [offset for _, _, offset in client.offsets[:3]] == [0, 50, 100]
    assert [offset for _, _, offset in client.offsets[3:6]] == [0, 50, 100]
    assert len(received) == 120
    assert len({message["message_id"] for message in received}) == 120
