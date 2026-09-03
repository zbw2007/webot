import threading
import time
import importlib
import concurrent.futures
import pytest
from types import SimpleNamespace

from src.wechat.wcdb_backend import WcdbBackend
from src.wechat.helpers import DedupSet

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


def test_start_releases_poll_leases_when_poll_loop_exits(monkeypatch):
    store = RecordingLeaseStore()
    client = SessionClient([{"username": "class@chatroom", "displayName": "Class"}])
    backend = WcdbBackend(groups=["class@chatroom"], store=store, poll_sec=0)
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: client)
    monkeypatch.setattr(backend._window, "find_hwnd", lambda: None)
    server = importlib.import_module("src.web.server")
    monkeypatch.setattr(server, "is_shutting_down", lambda: False)
    monkeypatch.setattr(backend, "_poll_cycle", lambda _callback: setattr(backend, "_running", False))

    backend.start(lambda _message: None)

    assert backend._leased_talkers == set()
    assert len(store.calls) == 1
    assert client.close_calls == 1


def test_start_releases_partial_leases_when_initial_acquisition_fails(monkeypatch):
    class LeaseStore:
        def __init__(self):
            self.acquired = []
            self.released = []

        def acquire_poll_lease(self, _backend, chat_id, _owner, _expires_at):
            self.acquired.append(chat_id)
            return True

        def release_poll_lease(self, _backend, chat_id, _owner):
            self.released.append(chat_id)

    store = LeaseStore()
    client = SessionClient([
        {"username": "one@chatroom", "displayName": "One"},
        {"username": "two@chatroom", "displayName": "Two"},
        {"username": "three@chatroom", "displayName": "Three"},
    ])
    backend = WcdbBackend(groups=["One", "Two", "Three"], store=store)
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: client)
    monkeypatch.setattr(backend._window, "find_hwnd", lambda: None)
    acquire_results = iter((True, True))

    def acquire_then_fail(talker):
        try:
            owned = next(acquire_results)
            if owned:
                backend._leased_talkers.add(talker)
            return owned
        except StopIteration:
            raise RuntimeError("lease failure")

    monkeypatch.setattr(backend, "_acquire_poll_lease", acquire_then_fail)

    backend.start(lambda _message: None)

    assert len(store.released) == 2
    assert backend._leased_talkers == set()
    assert backend._pool is None
    assert client.close_calls == 1


def test_start_cleans_up_when_thread_pool_construction_fails(monkeypatch):
    store = RecordingLeaseStore()
    client = SessionClient([{"username": "class@chatroom", "displayName": "Class"}])
    backend = WcdbBackend(groups=["Class"], store=store)
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: client)
    monkeypatch.setattr(backend._window, "find_hwnd", lambda: None)

    def fail_pool(*_args, **_kwargs):
        raise RuntimeError("pool failure")

    monkeypatch.setattr(wcdb_backend.concurrent.futures, "ThreadPoolExecutor", fail_pool)

    backend.start(lambda _message: None)

    assert backend._pool is None
    assert backend._leased_talkers == set()
    assert len(store.calls) == 1
    assert client.close_calls == 1


def test_poll_cycle_holds_lease_lock_while_snapshot_is_dispatched():
    entered_lookup = threading.Event()
    allow_lookup = threading.Event()
    reinit_started = threading.Event()
    reinit_finished = threading.Event()

    class CoordinatedMapping(dict):
        def get(self, key, default=None):
            entered_lookup.set()
            allow_lookup.wait(timeout=2)
            return super().get(key, default)

    backend = WcdbBackend(groups=["Class"], poll_sec=0)
    backend._running = True
    backend._talker_ids = CoordinatedMapping({"Class": "old@chatroom"})
    backend._poll_group_locked = lambda *_args: None

    def lifecycle_operation():
        with backend._lease_lock:
            reinit_started.set()
            reinit_finished.set()

    backend._reinitialize = lifecycle_operation

    polling = threading.Thread(target=backend._poll_cycle, args=(lambda _message: None,))
    polling.start()
    assert entered_lookup.wait(timeout=2)

    # A lifecycle operation must not pass the lease lock between mapping
    # snapshot and dispatch.  The stub represents reinitialization.
    lifecycle = threading.Thread(target=backend._reinitialize)
    lifecycle.start()
    time.sleep(0.05)
    assert not reinit_started.is_set()

    allow_lookup.set()
    polling.join(timeout=2)
    lifecycle.join(timeout=2)
    assert not polling.is_alive()
    assert not lifecycle.is_alive()
    assert reinit_finished.is_set()


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
    # The first multi-page scan performs a boundary verification at offset 0;
    # the next poll then reads its first page once.
    assert [offset for _, _, offset in client.offsets[3:]] == [0, 0]
    assert len(received) == 120
    assert len({message["message_id"] for message in received}) == 120


def test_poll_group_second_round_stops_at_cursor_and_reads_new_message():
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
    messages.append({"sender_username": "wxid_sender", "content": "new",
                     "timestamp": 120, "server_id": "120"})
    client.offsets.clear()
    backend._poll_group("class@chatroom", "class@chatroom", received.append)

    assert [offset for _, _, offset in client.offsets] == [0]
    assert [message["content"] for message in received].count("new") == 1
    assert len(received) == 121


def test_poll_group_rescans_when_newest_first_pagination_shifts():
    class InsertingPagingClient(PagingClient):
        def __init__(self, messages):
            super().__init__(messages)
            self.inserted = False

        def get_messages(self, talker, limit=50, offset=0):
            page = super().get_messages(talker, limit, offset)
            if offset == 50 and not self.inserted:
                self.messages.append({
                    "sender_username": "wxid_sender", "content": "arrived",
                    "timestamp": 121, "server_id": "arrived",
                })
                self.inserted = True
            return page

    messages = [
        {"sender_username": "wxid_sender", "content": f"message {i}",
         "timestamp": i, "server_id": str(i)}
        for i in range(120)
    ]
    client = InsertingPagingClient(messages)
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._client = client
    backend._running = True
    received = []
    backend._poll_group("class@chatroom", "class@chatroom", received.append)
    assert {message["content"] for message in received} == {
        *(f"message {i}" for i in range(120)), "arrived"
    }


def test_poll_group_restores_cursor_from_shared_store():
    class CursorStore:
        def __init__(self):
            self.cursors = {}

        def get_poll_cursor(self, chat_id):
            return self.cursors.get(chat_id)

        def save_poll_cursor(self, chat_id, timestamp, message_id):
            self.cursors[chat_id] = (timestamp, message_id)

        def get_sender_display_name(self, _sender):
            return None

    store = CursorStore()
    first_client = PagingClient([
        {"sender_username": "wxid_sender", "content": "old",
         "timestamp": 1, "server_id": "old"},
    ])
    first = WcdbBackend(groups=["class@chatroom"], store=store)
    first._client = first_client
    first._running = True
    first._poll_group("class@chatroom", "class@chatroom", lambda _: None)

    second_client = PagingClient(first_client.messages)
    second = WcdbBackend(groups=["class@chatroom"], store=store)
    second._client = second_client
    second._running = True
    received = []
    second._poll_group("class@chatroom", "class@chatroom", received.append)

    assert received == []
    assert second_client.offsets == [("class@chatroom", 50, 0)]


def test_poll_group_keeps_same_timestamp_messages_after_compound_cursor():
    import hashlib

    messages = [
        {"sender_username": "wxid_sender", "content": "first",
         "timestamp": 10, "server_id": "a"},
        {"sender_username": "wxid_sender", "content": "second",
         "timestamp": 10, "server_id": "b"},
    ]
    client = PagingClient(messages)
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._client = client
    backend._running = True
    received = []
    backend._poll_group("class@chatroom", "class@chatroom", received.append)

    cursor_id = backend._poll_cursors["class@chatroom"][1]
    new_server_id = next(
        candidate for candidate in (f"new-{i}" for i in range(1000))
        if hashlib.md5(candidate.encode()).hexdigest() > cursor_id
    )
    messages.append({"sender_username": "wxid_sender", "content": "third",
                     "timestamp": 10, "server_id": new_server_id})
    before = len(received)
    backend._poll_group("class@chatroom", "class@chatroom", received.append)

    assert len(received) == before + 1
    assert received[-1]["content"] == "third"


def test_poll_group_retries_when_callback_fails_before_delivery():
    client = PagingClient([{
        "sender_username": "wxid_sender", "content": "retry",
        "timestamp": 1, "server_id": "retry",
    }])
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._client = client
    backend._running = True
    attempts = []

    def callback(message):
        attempts.append(message["content"])
        if len(attempts) == 1:
            raise RuntimeError("temporary persistence failure")

    backend._poll_group("class@chatroom", "class@chatroom", callback)
    assert attempts == ["retry"]
    assert backend._poll_cursors == {}
    backend._poll_group("class@chatroom", "class@chatroom", callback)
    assert attempts == ["retry", "retry"]
    assert backend._poll_cursors["class@chatroom"][0] == 1


def test_poll_group_does_not_duplicate_inflight_callback():
    client = PagingClient([{
        "sender_username": "wxid_sender", "content": "slow",
        "timestamp": 1, "server_id": "slow",
    }])
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._client = client
    backend._running = True
    started = threading.Event()
    release = threading.Event()
    attempts = []

    def callback(message):
        attempts.append(message["content"])
        started.set()
        release.wait(timeout=2)

    worker = threading.Thread(target=backend._handle_message, args=(
        "class@chatroom", "class@chatroom",
        backend._standardize(client.messages[0], "class@chatroom", "class@chatroom"),
        callback,
    ))
    message_id = backend._standardize(
        client.messages[0], "class@chatroom", "class@chatroom"
    )["message_id"]
    backend._reserve_inflight("class@chatroom", message_id)
    worker.start()
    assert started.wait(timeout=1)
    backend._poll_group("class@chatroom", "class@chatroom", callback)
    release.set()
    worker.join(timeout=2)
    assert attempts == ["slow"]


def test_message_store_poll_cursor_is_monotonic_and_persistent(tmp_path):
    from src.db import MessageStore, initialize_db

    conn = initialize_db(str(tmp_path / "messages.db"))
    store = MessageStore(conn)
    assert store.get_poll_cursor("class@chatroom") is None
    assert store.save_poll_cursor("class@chatroom", 10, "z") is True
    assert store.save_poll_cursor("class@chatroom", 9, "later") is True
    assert store.get_poll_cursor("class@chatroom") == (10, "z")
    conn.close()
    conn2 = initialize_db(str(tmp_path / "messages.db"))
    assert MessageStore(conn2).get_poll_cursor("class@chatroom") == (10, "z")


def test_successful_messages_commit_only_through_contiguous_gap():
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._running = True
    first = {"timestamp": 1, "message_id": "first", "content": "first"}
    second = {"timestamp": 2, "message_id": "second", "content": "second"}
    backend._discover_position("class@chatroom", first)
    backend._discover_position("class@chatroom", second)
    assert backend._reserve_inflight("class@chatroom", first["message_id"])
    assert backend._reserve_inflight("class@chatroom", second["message_id"])

    backend._finish_inflight("class@chatroom", second["message_id"], second, True)
    assert backend._poll_cursors == {}
    assert ("class@chatroom", second["message_id"]) in backend._inflight

    backend._finish_inflight("class@chatroom", first["message_id"], first, True)
    assert backend._poll_cursors["class@chatroom"] == (2, second["message_id"])
    assert not backend._inflight


def test_cursor_persistence_failure_keeps_delivery_pending_without_callback_duplicate():
    class FlakyStore:
        def __init__(self):
            self.calls = 0
            self.cursor = None

        def save_poll_cursor(self, chat_id, timestamp, message_id):
            self.calls += 1
            if self.calls == 1:
                return False
            self.cursor = (timestamp, message_id)
            return True

        def get_poll_cursor(self, chat_id):
            return self.cursor

        def get_sender_display_name(self, sender):
            return None

    client = PagingClient([{
        "sender_username": "wxid_sender", "content": "retry-persist",
        "timestamp": 1, "server_id": "retry-persist",
    }])
    store = FlakyStore()
    backend = WcdbBackend(groups=["class@chatroom"], store=store)
    backend._client = client
    backend._running = True
    attempts = []

    backend._poll_group("class@chatroom", "class@chatroom", lambda message: attempts.append(message["content"]))
    assert attempts == ["retry-persist"]
    assert backend._poll_cursors == {}
    assert backend._inflight

    backend._poll_group("class@chatroom", "class@chatroom", lambda message: attempts.append(message["content"]))
    assert attempts == ["retry-persist"]
    assert backend._poll_cursors["class@chatroom"][0] == 1
    assert not backend._inflight


def test_cursor_save_false_reconciles_durable_cursor_from_another_instance():
    class ConcurrentStore:
        def __init__(self):
            self.cursor = None
            self.save_calls = 0
            self.get_calls = 0

        def save_poll_cursor(self, chat_id, timestamp, message_id):
            self.save_calls += 1
            self.cursor = (3, "already-committed")
            return False

        def get_poll_cursor(self, chat_id):
            self.get_calls += 1
            return self.cursor

        def get_sender_display_name(self, sender):
            return None

    store = ConcurrentStore()
    backend = WcdbBackend(groups=["class@chatroom"], store=store)
    backend._discover_position("class@chatroom", {
        "timestamp": 2, "message_id": "target", "content": "target",
    })
    backend._reserve_inflight("class@chatroom", "target")
    backend._finish_inflight("class@chatroom", "target", {
        "timestamp": 2, "message_id": "target", "content": "target",
    }, True)

    assert store.get_calls == 2
    assert backend._poll_cursors["class@chatroom"] == (3, "already-committed")
    assert not backend._inflight


def test_message_store_equal_cursor_is_idempotent_success(tmp_path):
    from src.db import MessageStore, initialize_db

    conn = initialize_db(str(tmp_path / "messages.db"))
    store = MessageStore(conn)
    assert store.save_poll_cursor("class@chatroom", 10, "same") is True
    assert store.save_poll_cursor("class@chatroom", 10, "same") is True
    assert store.save_poll_cursor("class@chatroom", 11, "newer") is True
    assert store.save_poll_cursor("class@chatroom", 10, "older") is True
    assert store.get_poll_cursor("class@chatroom") == (11, "newer")


def test_poll_lease_competes_renews_expires_and_releases(tmp_path):
    from src.db.schema import initialize_db
    from src.db.store import MessageStore

    conn = initialize_db(str(tmp_path / "leases.db"))
    first = MessageStore(conn)
    now = int(time.time())
    assert first.acquire_poll_lease("wcdb", "class@chatroom", "one", now + 300)
    assert not first.acquire_poll_lease("wcdb", "class@chatroom", "two", now + 300)
    assert first.acquire_poll_lease("wcdb", "class@chatroom", "one", now + 300)
    conn.execute(
        "UPDATE backend_poll_leases SET expires_at=0 "
        "WHERE backend='wcdb' AND chat_id='class@chatroom'"
    )
    conn.commit()
    assert first.acquire_poll_lease("wcdb", "class@chatroom", "two", now + 300)
    assert first.release_poll_lease("wcdb", "class@chatroom", "two")
    assert not first.release_poll_lease("wcdb", "class@chatroom", "one")


def test_poll_lease_is_shared_between_store_instances(tmp_path):
    from src.db.schema import initialize_db
    from src.db.store import MessageStore

    path = str(tmp_path / "leases.db")
    first_conn = initialize_db(path)
    second_conn = initialize_db(path)
    first = MessageStore(first_conn)
    second = MessageStore(second_conn)
    assert first.acquire_poll_lease("wcdb", "g", "one", 4_000_000_000)
    assert not second.acquire_poll_lease("wcdb", "g", "two", 4_000_000_000)
    assert first.release_poll_lease("wcdb", "g", "one")
    assert second.acquire_poll_lease("wcdb", "g", "two", 4_000_000_000)


class RecordingLeaseStore:
    def __init__(self):
        self.calls = []

    def acquire_poll_lease(self, backend, chat_id, owner, expires_at):
        self.calls.append((backend, chat_id, owner, expires_at))
        return True

    def release_poll_lease(self, backend, chat_id, owner):
        return True


class EmptyMessageClient:
    def get_messages(self, **_kwargs):
        return []

    def close(self):
        pass


def test_poll_group_renews_lease_before_each_read_with_minimum_ttl(monkeypatch):
    store = RecordingLeaseStore()
    backend = WcdbBackend(groups=["class@chatroom"], store=store, poll_sec=0)
    backend._client = EmptyMessageClient()
    backend._talker_ids = {"class@chatroom": "class@chatroom"}
    backend._running = True
    times = iter((1000, 1010))
    monkeypatch.setattr(wcdb_backend.time, "time", lambda: next(times))

    backend._poll_group("class@chatroom", "class@chatroom", lambda _message: None)
    backend._poll_group("class@chatroom", "class@chatroom", lambda _message: None)

    assert len(store.calls) == 2
    assert store.calls[1][3] > store.calls[0][3]
    assert store.calls[0][3] - 1000 >= 300
    assert store.calls[1][3] - 1010 >= 300


def test_stop_clears_pool_and_leases_even_when_release_fails(caplog):
    class FailingReleaseStore(RecordingLeaseStore):
        def release_poll_lease(self, *_args):
            raise RuntimeError("secret failure")

    store = FailingReleaseStore()
    backend = WcdbBackend(groups=["class@chatroom"], store=store)
    backend._leased_talkers = {"class@chatroom"}
    backend._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    with caplog.at_level("WARNING"):
        backend.stop()

    assert backend._pool is None
    assert backend._leased_talkers == set()
    assert "secret failure" not in caplog.text
    assert "class@chatroom" not in caplog.text
    assert "Failed to release polling lease" in caplog.text


def test_commit_contiguous_cleans_ledger_at_durable_cursor():
    backend = object.__new__(WcdbBackend)
    backend._poll_cursors = {"g": (0, "")}
    backend._discovered_positions = {"g": {(1, "a"): "a", (2, "b"): "b"}}
    backend._completed_positions = {"g": {(1, "a"), (2, "b")}}
    backend._pending_success = {"g": {(1, "a"): {}, (2, "b"): {}}}
    backend._inflight = {("g", "a"), ("g", "b")}
    backend._known_ids = DedupSet(max_size=100)
    backend._state_lock = threading.Lock()
    backend._store = None
    with backend._state_lock:
        backend._commit_contiguous_locked("g")
    assert backend._discovered_positions["g"] == {}
    assert backend._completed_positions["g"] == set()
    assert backend._pending_success["g"] == {}


def test_stop_closes_send_gate_before_reply_worker_can_send():
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._running = True
    callback_ready = threading.Event()
    callback_release = threading.Event()
    sent = []
    standardized = {"timestamp": 1, "message_id": "m1", "content": "hello"}

    def callback(_message):
        callback_ready.set()
        callback_release.wait(timeout=2)
        return "reply"

    backend._send_and_confirm = lambda *_args: sent.append(True) or True
    worker = threading.Thread(target=backend._handle_message, args=(
        "class@chatroom", "class@chatroom", standardized, callback,
    ))
    worker.start()
    assert callback_ready.wait(timeout=1)
    backend.stop()
    callback_release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert sent == []


def test_stop_cancels_queued_callback_futures():
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._running = True
    backend._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()

    running = backend._pool.submit(lambda: (started.set(), release.wait(timeout=2)))
    queued = backend._pool.submit(lambda: "must not run")
    assert started.wait(timeout=1)
    backend.stop()
    release.set()
    running.result(timeout=2)
    assert queued.cancelled()


def test_lifecycle_lock_order_does_not_deadlock(monkeypatch):
    backend = WcdbBackend(groups=["class@chatroom"])
    backend._running = True
    backend._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    class FakeClient:
        def init(self):
            pass

        def open(self):
            pass

        def close(self):
            pass

    backend._client = FakeClient()
    backend._talker_ids = {"class@chatroom": "class@chatroom"}
    backend._resolve_groups = lambda: None
    backend._acquire_poll_lease = lambda _talker: True
    backend._release_poll_leases = lambda: None
    backend._window.find_hwnd = lambda: None
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", FakeClient)

    threads = [threading.Thread(target=backend.stop),
               threading.Thread(target=backend._reinitialize)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
