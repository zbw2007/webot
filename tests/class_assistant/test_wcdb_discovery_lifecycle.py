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
