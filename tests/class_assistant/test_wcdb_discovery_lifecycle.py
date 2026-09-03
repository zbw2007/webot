import threading
import time
import importlib

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
