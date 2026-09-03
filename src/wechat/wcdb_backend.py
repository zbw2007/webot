"""
WCDB Native Backend — zero external dependencies.

Reads WeChat messages directly from the encrypted WCDB database via
patched wcdb_api.dll (ctypes).  Uses WeChatWindowController for sending.

WCDB native access — reads encrypted database in-process, no external dependencies.
"""
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from .base import AbstractWeChatBackend, MessageCallback
from .wcdb_client import WcdbNativeClient
from .window_controller import WeChatWindowController
from .helpers import DedupSet
from src.class_assistant.whitelist import is_auto_discovery_token
from src.class_assistant.group_discovery import discover_groups

logger = logging.getLogger(__name__)

DEFAULT_POLL_SEC = 1.0
MAX_DEDUP_SIZE = 5000
MAX_CONSECUTIVE_ERRORS = 5   # trigger reinit after this many consecutive failures


class WcdbBackend(AbstractWeChatBackend):
    """Native WCDB backend — database read + window send.

    Reads messages directly from WeChat's session.db via wcdb_api.dll
    with one-byte DRM patch. Sends via WeChatWindowController.

    Usage:
        backend = WcdbBackend(
            bot_display_name="机器人",
            groups=["摸鱼群"],
            poll_sec=1.0,
        )
        backend.start(my_callback)
    """

    def __init__(self,
                 bot_display_name: str = "",
                 groups: list[str] | None = None,
                 poll_sec: float = DEFAULT_POLL_SEC,
                 store=None,
                 config=None):
        self._bot_name = bot_display_name
        self._groups = groups or []
        if config is not None and getattr(config, "class_assistant_enabled", False):
            try:
                configured_groups = list(self._groups)
            except TypeError:
                configured_groups = []
            if not configured_groups or any(
                not isinstance(group, str) or not group.strip()
                or is_auto_discovery_token(group) for group in configured_groups
            ):
                raise ValueError("class assistant groups must be explicit non-wildcard strings")
            self._groups = configured_groups
        self._poll_sec = poll_sec
        self._store = store  # MessageStore fallback for name resolution
        self._running = False
        self._client: Optional[WcdbNativeClient] = None
        self._window = WeChatWindowController()
        self._talker_ids: dict[str, str] = {}
        self._known_ids = DedupSet(max_size=MAX_DEDUP_SIZE)
        # Last delivered (timestamp, message id) per group.  The in-memory
        # DedupSet remains the authoritative guard for replayed pages.
        self._poll_cursors: dict[str, tuple[int, str]] = {}
        self._state_lock = threading.Lock()
        self._inflight: set[tuple[str, str]] = set()
        # Per-talker delivery ledger.  A cursor may advance only through
        # positions that were observed and completed without a gap.
        self._discovered_positions: dict[str, dict[tuple[int, str], str]] = {}
        self._completed_positions: dict[str, set[tuple[int, str]]] = {}
        self._pending_success: dict[str, dict[tuple[int, str], dict]] = {}
        # Thread safety: WCDB DLL (ctypes) may not be thread-safe internally.
        # All _client calls are serialized through this lock.
        self._client_lock = threading.Lock()
        # Sending is independently gated from native-client access.  Lifecycle
        # shutdown closes this gate before cancelling queued workers.
        self._send_lock = threading.Lock()
        # Callback thread pool — fire-and-forget AI calls so the poll loop
        # never blocks on a slow summarization.
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        # Voice recognition pipeline (lazy-init when voice_asr_enabled)
        self._voice: Optional[object] = None
        self._voice_config = config
        # Unique per-process owner token.  It is intentionally never logged.
        self._lease_owner = uuid.uuid4().hex
        self._lease_backend = "wcdb"
        self._lease_ttl_sec = 300
        self._leased_talkers: set[str] = set()
        # Lease state is shared by polling and lifecycle operations.  Keep it
        # independent from the native-client lock so lease release cannot race
        # a poll that is about to renew ownership.
        self._lease_lock = threading.RLock()

    def _load_poll_cursor(self, talker: str) -> tuple[int, str]:
        """Load an optional cursor from the injected store.

        The backend itself only guarantees in-process cursors.  A store may
        opt into restart recovery by implementing this tiny interface.
        """
        if talker in self._poll_cursors:
            return self._poll_cursors[talker]
        getter = getattr(self._store, "get_poll_cursor", None)
        if not callable(getter):
            return (0, "")
        try:
            value = getter(talker)
            if value is None:
                return (0, "")
            timestamp, message_id = value
            cursor = (int(timestamp), str(message_id))
            self._poll_cursors[talker] = cursor
            return cursor
        except (TypeError, ValueError, KeyError):
            return (0, "")

    def _save_poll_cursor(self, talker: str, cursor: tuple[int, str]) -> bool:
        saver = getattr(self._store, "save_poll_cursor", None)
        if callable(saver):
            try:
                result = saver(talker, cursor[0], cursor[1])
                if result is False:
                    # A concurrent instance may have committed the same or a
                    # newer position while this call reported "not changed".
                    # Bypass the in-memory cache and reconcile from durable
                    # storage so completed messages do not remain inflight.
                    getter = getattr(self._store, "get_poll_cursor", None)
                    if not callable(getter):
                        return False
                    value = getter(talker)
                    if value is None:
                        return False
                    durable = (int(value[0]), str(value[1]))
                    if durable < cursor:
                        return False
                    self._poll_cursors[talker] = durable
                    return True
            except Exception:
                logger.warning("Failed to persist polling cursor")
                return False
        self._poll_cursors[talker] = cursor
        return True

    def _discover_position(self, talker: str, standardized: dict) -> tuple[int, str]:
        position = (int(standardized["timestamp"]), str(standardized["message_id"]))
        with self._state_lock:
            self._discovered_positions.setdefault(talker, {})[position] = str(
                standardized["message_id"]
            )
        return position

    def _reserve_inflight(self, talker: str, message_id: str,
                          standardized: Optional[dict] = None) -> bool:
        key = (talker, message_id)
        with self._state_lock:
            if message_id in self._known_ids or key in self._inflight:
                return False
            if standardized is not None:
                position = (int(standardized["timestamp"]), str(message_id))
                self._discovered_positions.setdefault(talker, {})[position] = str(message_id)
            self._inflight.add(key)
            return True

    def _commit_contiguous_locked(self, talker: str) -> None:
        """Persist the furthest completed contiguous position, if any.

        Caller holds ``_state_lock``.  Keeping inflight reservations until
        persistence succeeds closes the duplicate-dispatch window.
        """
        cursor = self._load_poll_cursor(talker)
        discovered = self._discovered_positions.get(talker, {})
        completed = self._completed_positions.get(talker, set())
        pending = self._pending_success.get(talker, {})
        contiguous = [position for position in sorted(discovered)
                      if position > cursor and position in completed]
        if not contiguous:
            return
        # A missing completed position is a delivery gap; do not jump over it.
        expected = []
        for position in sorted(discovered):
            if position <= cursor:
                continue
            if position not in completed:
                break
            expected.append(position)
        if not expected:
            return
        target = expected[-1]
        if not self._save_poll_cursor(talker, target):
            return
        for position in expected:
            message_id = discovered[position]
            self._known_ids.add(message_id)
            completed.discard(position)
            pending.pop(position, None)
            self._inflight.discard((talker, message_id))
        # The durable cursor is the source of truth after a successful commit;
        # discard old ledger entries so long-running polling stays bounded.
        for position in list(discovered):
            if position <= target:
                discovered.pop(position, None)
        for position in list(completed):
            if position <= target:
                completed.discard(position)
        for position in list(pending):
            if position <= target:
                pending.pop(position, None)

    def _retry_pending_success(self, talker: str) -> None:
        with self._state_lock:
            self._commit_contiguous_locked(talker)

    def _finish_inflight(self, talker: str, message_id: str,
                         standardized: dict, success: bool) -> None:
        key = (talker, message_id)
        with self._state_lock:
            if not success:
                self._inflight.discard(key)
                return
            position = (int(standardized["timestamp"]), str(message_id))
            self._discovered_positions.setdefault(talker, {})[position] = str(message_id)
            self._completed_positions.setdefault(talker, set()).add(position)
            self._pending_success.setdefault(talker, {})[position] = standardized
            self._commit_contiguous_locked(talker)

    # ── Public API ─────────────────────────────────────────────────

    def _acquire_poll_lease(self, talker: str) -> bool:
        with self._lease_lock:
            acquire = getattr(self._store, "acquire_poll_lease", None)
            if not callable(acquire):
                return True
            try:
                expires_at = int(time.time()) + max(300, self._lease_ttl_sec)
                owned = bool(acquire(self._lease_backend, talker,
                                     self._lease_owner, expires_at))
            except Exception:
                owned = False
            if owned:
                self._leased_talkers.add(talker)
            else:
                self._leased_talkers.discard(talker)
            return owned

    def _release_poll_leases(self) -> None:
        with self._lease_lock:
            release = getattr(self._store, "release_poll_lease", None)
            if not callable(release):
                self._leased_talkers.clear()
                return
            for talker in list(self._leased_talkers):
                try:
                    release(self._lease_backend, talker, self._lease_owner)
                except Exception:
                    # Never expose target, owner, or store exception details.
                    logger.warning("Failed to release polling lease")
            self._leased_talkers.clear()

    def start(self, callback: MessageCallback) -> None:
        if not self._groups:
            logger.error("No groups configured. Set WECHAT_GROUPS in .env")
            return

        logger.info("WcdbBackend starting; groups=%d", len(self._groups))

        # Init and open database
        try:
            # Lifecycle lock order is always lease -> client.  This keeps
            # startup consistent with polling, reinitialization, and stop.
            with self._lease_lock:
                with self._client_lock:
                    self._client = WcdbNativeClient()
                    self._client.init()
                    self._client.open()
                    self._resolve_groups()
                    if not self._talker_ids:
                        logger.error("No groups resolved. Check WECHAT_GROUPS.")
                        self._close_client_locked()
                        return
                for talker in set(self._talker_ids.values()):
                    self._acquire_poll_lease(talker)
            logger.info("WCDB database opened successfully")
        except BaseException as e:
            # Initialization may have acquired some leases before a later
            # group fails.  Always release those leases, including when the
            # caller interrupts startup, before propagating fatal signals.
            with self._lease_lock:
                self._release_poll_leases()
                with self._client_lock:
                    self._close_client_locked()
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("WCDB initialization failed")
            try:
                from src.web.server import update_status
                update_status(running=False, error="WCDB initialization failed")
            except Exception:
                pass
            return

        # Pre-find WeChat window
        hwnd = self._window.find_hwnd()
        if hwnd:
            logger.info("WeChat window pre-detected")
        else:
            logger.warning("WeChat window not found — will retry on first send")

        try:
            self._pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="bot-cb-",
            )
            with self._send_lock:
                self._running = True
            consecutive_errors = 0

            # Import once to avoid per-iteration overhead
            from src.web.server import is_shutting_down as _is_shutting_down

            while self._running and not _is_shutting_down():
                try:
                    self._poll_cycle(callback)
                    consecutive_errors = 0
                except KeyboardInterrupt:
                    break
                except Exception:
                    consecutive_errors += 1

                    # After MAX_CONSECUTIVE_ERRORS consecutive failures,
                    # attempt full reinitialization (WeChat may have restarted).
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        logger.error(
                            "Hit %d consecutive errors — attempting "
                            "reinitialization...", consecutive_errors,
                        )
                        try:
                            self._reinitialize()
                            consecutive_errors = 0
                            continue
                        except Exception:
                            logger.error("WCDB reinitialization failed")
                            # Fall through to backoff; will retry next cycle.
                            push_error = "WCDB reinitialization failed"
                            try:
                                from src.web.server import update_status
                                update_status(error=push_error)
                            except Exception:
                                pass

                    wait = min(2 ** min(consecutive_errors % MAX_CONSECUTIVE_ERRORS, 5), 30)
                    logger.warning(
                        "WCDB poll error #%d. Retry in %ss...",
                        consecutive_errors, wait,
                    )
                    time.sleep(wait)
        except Exception:
            # Startup failures (most notably executor construction) are
            # reported without exposing implementation or credential details;
            # the finally block below performs all resource cleanup.
            logger.error("WCDB runtime startup failed")
            try:
                from src.web.server import update_status
                update_status(running=False, error="WCDB runtime startup failed")
            except Exception:
                pass
        finally:
            with self._lease_lock:
                # Close the send gate before draining/cancelling workers.
                with self._send_lock:
                    self._running = False
                    pool = self._pool
                    self._pool = None
                if pool is not None:
                    # Drain in-flight callbacks gracefully.  Cleanup continues
                    # if an executor implementation itself raises during shutdown.
                    try:
                        pool.shutdown(wait=True, cancel_futures=True)
                    except Exception:
                        logger.warning("Failed to shut down callback pool")
                with self._client_lock:
                    self._close_client_locked()
                # Release leases even if shutdown, client close, or the poll
                # loop exited through an exception or KeyboardInterrupt.
                self._release_poll_leases()
        logger.info("WcdbBackend stopped.")

    def _close_client_locked(self) -> None:
        """Close the WCDB client; caller must hold ``_client_lock``."""
        client = self._client
        self._client = None
        # Talker IDs belong to the client/session snapshot.  Never leave
        # mappings from a closed client usable by send or polling paths.
        self._talker_ids.clear()
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def send_text(self, chat_id: str, content: str) -> bool:
        if not content:
            return False

        with self._send_lock:
            if not self._running:
                return False
            with self._client_lock:
                group_name = self._talker_to_name_locked(chat_id)
            if not group_name:
                logger.error("Cannot resolve send target")
                return False
            return self._send_and_confirm(group_name, chat_id, content)

    def validate_send_target(self, chat_id: str, group_name: str) -> bool:
        """Validate the backend-owned WeChat window before a real send.

        The browser/API cannot supply a trustworthy window title.  Resolve the
        chat id through WCDB and validate the currently visible WeChat window
        here, immediately before the sender claims a draft.
        """
        if not chat_id or not group_name:
            return False
        try:
            with self._client_lock:
                resolved = self._talker_to_name_locked(chat_id)
            if resolved != group_name:
                return False
            hwnd = self._window.find_hwnd()
            if not hwnd or not self._window._validate_hwnd(hwnd):
                return False
            if not self._window._foreground_matches(hwnd):
                return False
            return bool(self._window._verify_chat_title(hwnd, group_name))
        except Exception:
            logger.error("Failed to validate real-send target")
            return False

    def stop(self) -> None:
        with self._lease_lock:
            # Acquire the same gate used by reply workers.  A worker that has
            # not entered the sender yet will observe stopped state and skip.
            with self._send_lock:
                self._running = False
                pool = self._pool
                self._pool = None
            if pool:
                pool.shutdown(wait=False, cancel_futures=True)
            with self._client_lock:
                self._close_client_locked()
            self._release_poll_leases()

    # ── Recovery ─────────────────────────────────────────────────────

    def _reinitialize(self) -> None:
        # Keep release, client replacement, and reacquisition atomic with
        # respect to polling lease operations.
        with self._lease_lock:
            self._reinitialize_locked()

    def _reinitialize_locked(self) -> None:
        """Close and re-open the WCDB client after persistent errors.

        Called when the poll loop hits MAX_CONSECUTIVE_ERRORS consecutive
        failures — typically because WeChat was restarted and the DB handle
        or HWND became stale.
        """
        logger.warning("Reinitializing WCDB backend after consecutive errors...")
        self._release_poll_leases()
        with self._client_lock:
            self._close_client_locked()
            try:
                self._client = WcdbNativeClient()
                self._client.init()
                self._client.open()
                logger.info("WCDB reinitialized successfully")
                # Clear dedup set — WCDB may return messages with new IDs.
                # Poll cursors remain per talker so reinitialization does not
                # replay the entire history; persisted stores can also restore
                # them when a new backend instance is created.
                self._known_ids = DedupSet(max_size=MAX_DEDUP_SIZE)
                # Re-resolve groups (talker IDs may have changed)
                self._resolve_groups()
                for talker in set(self._talker_ids.values()):
                    self._acquire_poll_lease(talker)
            except Exception as e:
                logger.error("WCDB reinitialization failed")
                # A partially initialized replacement client is unsafe to
                # retain, and its mappings must never fall back to the old
                # client snapshot.
                self._close_client_locked()
                raise
        # Re-find WeChat window
        hwnd = self._window.find_hwnd()
        if hwnd:
            logger.info("WeChat window re-detected: HWND=%s", hwnd)
        else:
            logger.warning("WeChat window not found after reinit")

    # ── Group resolution ────────────────────────────────────────────

    def _resolve_groups(self) -> None:
        """Map configured group names to talker IDs from WCDB sessions.

        WCDB session records only contain usernames (e.g. 20968749111@chatroom).
        Display names must be resolved via the DLL's get_display_names() or
        the local nickname cache (WeChat contacts / manual overrides).
        """
        # A reinitialization may discover a different set of sessions.  Never
        # let IDs from the previous client remain usable when resolution
        # fails or yields no matching groups.
        self._talker_ids.clear()
        sessions = self._client.get_sessions()

        class_mode = bool(getattr(self._voice_config, "class_assistant_enabled", False))
        # In class-assistant mode the configured values are stable IDs, never
        # display names or discovery tokens.  Filter before any member/name
        # lookup so non-whitelisted groups are not even read.
        whitelist = {
            group.strip() for group in self._groups
            if isinstance(group, str) and group.strip().endswith("@chatroom")
        } if class_mode else set()

        # Build a map of permitted @chatroom entries: username -> {name, member_count}
        all_chatrooms: dict[str, dict] = {}
        for s in sessions:
            username = str(s.get("username", "") or "")
            if not username.endswith("@chatroom"):
                continue
            if class_mode and username not in whitelist:
                continue

            # Try session-level display name fields (rarely populated)
            display = str(
                s.get("displayName") or s.get("displayname")
                or s.get("nickname") or s.get("display_name")
                or ""
            ).strip()
            if not display:
                # Fall back to DLL lookup (resolves via contacts DB + nicknames)
                display = self._client.resolve_nickname(username)
            if not display or display == username:
                # Last resort: try last_sender_display_name from session,
                # or use the numeric prefix of username as label
                display = str(s.get("last_sender_display_name", "") or "").strip()
                if not display or display == username:
                    display = username  # fallback

            # Get real member count from group member list
            member_count = 0
            try:
                members = self._client.get_group_members(username)
                if members:
                    member_count = len(members)
            except Exception:
                pass

            all_chatrooms[username] = {
                "name": display,
                "member_count": member_count,
            }

        if not all_chatrooms:
            logger.error(
                "No @chatroom sessions found in WCDB (total sessions: %d). "
                "Make sure WeChat is logged in and session.db is accessible.",
                len(sessions),
            )
            return

        auto_discover = (
            not self._groups
            or (len(self._groups) == 1 and isinstance(self._groups[0], str)
                and (not self._groups[0].strip() or is_auto_discovery_token(self._groups[0])))
        )

        if class_mode and auto_discover:
            logger.error("Class assistant groups must be explicit; refusing auto-discovery")
            return

        resolved_talker_ids: dict[str, str] = {}
        if auto_discover:
            for username, info in all_chatrooms.items():
                resolved_talker_ids[info["name"]] = username
            logger.info("Auto-discovered group chats: count=%d", len(resolved_talker_ids))
            self._groups = list(resolved_talker_ids.keys())

        else:
            # Manual mode: stable chat IDs are preferred.  Display-name
            # matching is retained for legacy configuration, but ambiguous
            # names are rejected instead of selecting an arbitrary chat.
            for group_name in self._groups:
                # Direct lookup: maybe group_name IS a stable username such
                # as 20968749111@chatroom.
                if group_name in all_chatrooms:
                    resolved_talker_ids[group_name] = group_name
                    display = all_chatrooms[group_name]["name"]
                    if display and display != group_name:
                        resolved_talker_ids[display] = group_name
                    logger.info("Resolved configured group")
                    continue

                exact = [username for username, info in all_chatrooms.items()
                         if group_name.casefold() == info["name"].casefold()]
                candidates = exact or [
                    username for username, info in all_chatrooms.items()
                    if group_name.casefold() in info["name"].casefold()
                    or info["name"].casefold() in group_name.casefold()
                ]
                if len(candidates) == 1:
                    found = candidates[0]
                    resolved_talker_ids[group_name] = found
                    logger.info("Resolved configured group")
                elif len(candidates) > 1:
                    logger.error("Refusing ambiguous configured group; matches=%d", len(candidates))
                else:
                    logger.warning("Could not resolve configured group")

        # Commit only after complete resolution so an exception cannot expose
        # a partial mapping from this client.
        self._talker_ids = resolved_talker_ids

        # Persist chat_id -> display_name so the web UI can show
        # human-readable group names in the nickname dropdown.
        if all_chatrooms:
            self._save_group_names(all_chatrooms)

        # ── Resolve and persist group members ──────────────────────────
        group_members: dict[str, dict[str, str]] = {}
        for username, info in all_chatrooms.items():
            try:
                members = self._client.get_group_members(username)
                if not members:
                    continue
                wxids = [m.get("username", "") for m in members if m.get("username")]
                if not wxids:
                    continue
                # Resolve display names in batches of 200
                names = {}
                for i in range(0, len(wxids), 200):
                    batch = wxids[i:i + 200]
                    names.update(self._client.get_display_names(batch))
                # Filter out unresolved (where name == wxid) and save
                group_members[username] = {
                    wxid: names.get(wxid, wxid)
                    for wxid in wxids
                }
                logger.info("Resolved %d/%d member names", len(group_members[username]), len(wxids))
            except Exception:
                logger.warning("Failed to resolve group members")
        if group_members:
            self._save_group_members(group_members)

    def discover_group_metadata(self) -> list[dict]:
        """Discover group metadata under the native client lock."""
        with self._client_lock:
            if self._client is None:
                raise RuntimeError("WCDB backend is not ready")
            return discover_groups(self._client)

    @staticmethod
    def _save_group_members(chat_members: dict[str, dict[str, str]]) -> None:
        """Persist chat_id -> {wxid: display_name} to data/group_members.json."""
        import os as _os
        path = Path("data/group_members.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            data = json.dumps(chat_members, ensure_ascii=False, indent=2)
            tmp_path.write_text(data, encoding="utf-8")
            _os.replace(tmp_path, path)
            total = sum(len(m) for m in chat_members.values())
            logger.info("Saved %d member names across %d groups", total, len(chat_members))
        except Exception:
            logger.warning("Failed to persist group members")

    @staticmethod
    def _save_group_names(chatrooms: dict[str, dict]) -> None:
        """Persist chat_id -> {name, member_count} to data/group_names.json atomically."""
        import os as _os
        path = Path("data/group_names.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            data = json.dumps(chatrooms, ensure_ascii=False, indent=2)
            tmp_path.write_text(data, encoding="utf-8")
            _os.replace(tmp_path, path)
            logger.info("Saved %d group-name mappings", len(chatrooms))
        except Exception:
            logger.warning("Failed to persist group names")

    def _talker_to_name_locked(self, talker_id: str) -> str:
        """Resolve a talker; caller must hold ``_client_lock``."""
        fallback = ""
        for name, tid in self._talker_ids.items():
            if tid == talker_id:
                if not fallback:
                    fallback = name
                if name != talker_id:
                    return name
        return fallback

    def _talker_to_name(self, talker_id: str) -> str:
        with self._client_lock:
            return self._talker_to_name_locked(talker_id)

    # ── Message polling ──────────────────────────────────────────────

    def _poll_cycle(self, callback: MessageCallback) -> None:
        for group_name in list(self._groups):
            # Take the client-session mapping snapshot and dispatch it while
            # holding the same lifecycle lock.  Reinitialization/stop cannot
            # then replace the client or clear leases between those actions.
            with self._lease_lock:
                if not self._running:
                    break
                talker = self._talker_ids.get(group_name)
                if not talker:
                    continue
                self._poll_group_locked(group_name, talker, callback)
        # Check shutdown signal before sleeping so stop() is responsive
        if not self._running:
            return
        time.sleep(self._poll_sec)

    def _poll_group(self, group_name: str, talker: str,
                    callback: MessageCallback) -> None:
        # Hold the lease lifecycle lock for the complete read/dispatch cycle:
        # stop/reinitialize cannot release ownership while native reads are in
        # flight, and a stopped backend cannot renew after release.
        with self._lease_lock:
            self._poll_group_locked(group_name, talker, callback)

    def _poll_group_locked(self, group_name: str, talker: str,
                           callback: MessageCallback) -> None:
        """Fetch messages for one group and dispatch new ones.

        AI-triggering callbacks are submitted to the thread pool so slow
        summarization in one group never blocks polling of other groups.
        """
        # A lease-capable store is fail-closed: never read without ownership.
        if not self._acquire_poll_lease(talker):
            return
        # WCDB returns newest-first pages.  Once a page contains a message at
        # or before the per-group cursor, older pages cannot contain new
        # messages.  The complete boundary page is still examined so messages
        # sharing a timestamp are not skipped.
        self._retry_pending_success(talker)
        page_size = 50
        cursor = self._load_poll_cursor(talker)
        standardized_messages = []
        # Offset pagination is unstable when new rows arrive at the front.
        # Rescan if the first-page boundary moved while this snapshot was read.
        for _attempt in range(3):
            offset = 0
            standardized_messages = []
            first_boundary = None
            while True:
                with self._client_lock:
                    page = self._client.get_messages(
                        talker=talker, limit=page_size, offset=offset,
                    )
                if not page:
                    break
                if offset == 0 and page:
                    first_boundary = repr(page[0])
                offset += len(page)
                page_records = []
                for raw in page:
                    standardized = self._standardize(raw, group_name, talker)
                    if standardized is not None:
                        self._discover_position(talker, standardized)
                        page_records.append(standardized)
                standardized_messages.extend(page_records)
                if cursor != (0, "") and any(
                    (int(item["timestamp"]), str(item["message_id"])) <= cursor
                    for item in page_records
                ):
                    break
                if len(page) < page_size:
                    break
            if offset <= page_size:
                break
            with self._client_lock:
                check = self._client.get_messages(talker=talker, limit=1, offset=0)
            if not check or repr(check[0]) == first_boundary:
                break
        if not standardized_messages:
            return

        candidates = []
        for standardized in standardized_messages:
            position = (int(standardized["timestamp"]),
                        str(standardized["message_id"]))
            if cursor != (0, "") and position <= cursor:
                continue
            candidates.append(standardized)

        for standardized in sorted(
            candidates, key=lambda item: (int(item["timestamp"]),
                                          str(item["message_id"])),
        ):
            if not self._running:
                break

            msg_id = standardized["message_id"]
            if not self._reserve_inflight(talker, msg_id, standardized):
                continue

            if self._bot_name and self._bot_name in standardized["sender_name"]:
                self._finish_inflight(talker, msg_id, standardized, True)
                continue

            self._trim_dedup()

            # Fire-and-forget: callback (potentially AI call) + send run in
            # a thread pool worker so the poll loop continues immediately.
            if self._pool:
                self._pool.submit(
                    self._handle_message,
                    group_name, talker, standardized, callback,
                )
            else:
                # Fallback (pool already shut down): run inline
                self._handle_message(
                    group_name, talker, standardized, callback,
                )

    def _handle_message(self, group_name: str, talker: str,
                        standardized: dict, callback: MessageCallback) -> None:
        """Execute callback and send reply (runs in thread pool worker)."""
        # Keep direct test/integration calls safe: normal polling reserves
        # first, while direct callers get the same at-most-once reservation.
        key = (talker, standardized["message_id"])
        with self._state_lock:
            reserved = key in self._inflight
            known = standardized["message_id"] in self._known_ids
        if not reserved and not known and not self._reserve_inflight(
                talker, standardized["message_id"], standardized):
            return
        if not self._running:
            self._finish_inflight(talker, standardized["message_id"], standardized, False)
            return

        success = False
        try:
            cb_start = time.monotonic()
            reply = callback(standardized)
            cb_elapsed = time.monotonic() - cb_start
            if cb_elapsed > 0.5:
                logger.debug("Callback exceeded expected duration: %.2fs", cb_elapsed)

            if reply:
                logger.info("Reply ready; chars=%d", len(reply))
                # _send_and_confirm uses window_controller (keyboard), not
                # _client (WCDB).  Don't hold _client_lock during send —
                # it blocks the poll loop from reading new messages.
                with self._send_lock:
                    if not self._running:
                        sent = False
                    else:
                        sent = self._send_and_confirm(group_name, talker, reply)
                if sent:
                    success = True
                    logger.info("Reply sent; chars=%d", len(reply))
                else:
                    logger.error("Reply failed; check WeChat window")
            else:
                with self._send_lock:
                    success = self._running
        except Exception:
            logger.error("Unhandled error in callback worker")
        finally:
            self._finish_inflight(talker, standardized["message_id"], standardized, success)

    # ── Voice recognition helpers ────────────────────────────────────

    def _get_voice(self):
        """Lazy-init the VoicePipeline (avoids import unless enabled)."""
        if self._voice is not None:
            return self._voice
        if self._voice_config is None:
            self._voice = False  # Sentinel: no config → disabled
            return False
        try:
            from src.voice import VoicePipeline
            self._voice = VoicePipeline(self._voice_config)
        except Exception:
            logger.error("Voice pipeline initialization failed; voice disabled")
            self._voice = False
        return self._voice

    def _try_voice(self, msg: dict) -> Optional[str]:
        """Attempt voice recognition; return text or None on failure."""
        voice = self._get_voice()
        if not voice:  # False or None → disabled
            return None
        try:
            return voice.process(msg)
        except Exception:
            logger.error("Voice pipeline processing failed")
            return None

    # ── Message standardization ──────────────────────────────────────

    def _standardize(self, msg: dict, group_name: str,
                     talker: str) -> Optional[dict]:
        """Convert WCDB raw message to standard format."""
        # WCDB message fields: sender_username, message_content, local_type, create_time
        sender = str(msg.get("sender_username", msg.get("senderUsername", msg.get("sender", ""))))
        content = str(msg.get("message_content", msg.get("content", ""))).strip()
        local_type = int(msg.get("localType", msg.get("msg_type", 1)))

        # ── Voice recognition ──────────────────────────────────────
        # Voice messages (localType=34) have empty message_content;
        # we must recognise them BEFORE the empty-content check below.
        if local_type == 34:
            voice_text = self._try_voice(msg)
            if voice_text:
                content = f"[语音] {voice_text}"
            else:
                content = "[语音]"
        elif not content:
            return None

        # ── System message handling ───────────────────────────────
        # Extract "xxx joined the group" events → welcome feature.
        # Only active when WELCOME_ENABLED=true; otherwise join messages
        # are silently filtered out like other system messages.
        _JOIN_PATTERN = re.compile(r'"([^"]+)"(?:通过[^"]*)?加入了群聊')
        join_match = _JOIN_PATTERN.search(content)
        new_member_id: str = ""
        is_system_join: bool = False
        welcome_on = (
            self._voice_config is not None
            and getattr(self._voice_config, "welcome_enabled", False)
        )
        if join_match and welcome_on:
            new_member_id = join_match.group(1)
            is_system_join = True
            logger.info("Join event detected")
        elif join_match:
            # Welcome disabled — silently drop join messages
            return None

        # Filter other system messages (but NOT join events)
        if not is_system_join:
            _FILTER_KEYWORDS = (
                "修改群名", "退出了群聊",
                "撤回了一条消息", "被移除", "开启了朋友验证",
                "移出了群聊",
            )
            # NOTE: "邀请" intentionally NOT in this list — it would
            # false-positive filter normal chat like "我邀请你参加活动".
            # System invite messages ("xxx邀请yyy加入了群聊") are already
            # caught by _JOIN_PATTERN above.
            if any(kw in content for kw in _FILTER_KEYWORDS):
                return None

        # Parse timestamp
        ts = msg.get("create_time", msg.get("createTime", msg.get("timestamp", 0)))
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            ts = int(time.time())

        # Resolve sender display name
        with self._client_lock:
            sender_name = self._client.resolve_nickname(sender)

        # Fallback: if WCDB DLL can't resolve (user not in contacts),
        # try the messages table for a previously seen display name
        if sender_name == sender and self._store is not None:
            prev = self._store.get_sender_display_name(sender)
            if prev:
                sender_name = prev

        # Resolve @mentions in content
        resolved_content = content
        if "@" in content:
            def _replace_at(match):
                at_wxid = match.group(0)[1:]
                with self._client_lock:
                    name = self._client.resolve_nickname(at_wxid)
                return f"@{name}" if name != at_wxid else match.group(0)
            resolved_content = re.sub(r'@wxid_[a-zA-Z0-9]+', _replace_at, content)

        # Detect @mention of bot
        is_at = self._bot_name and (
            f"@{self._bot_name}" in resolved_content
            or f"@{self._bot_name}" in content
        )

        # Generate stable message ID.
        # For system join messages, use a content-based ID to avoid
        # volatile WCDB server_id/local_id causing dedup misses.
        if is_system_join:
            raw_id = f"join|{talker}|{new_member_id}|{content}|{ts}"
        else:
            raw_id = (
                str(msg.get("server_id", ""))
                or str(msg.get("local_id", ""))
                or f"{sender}|{content}|{ts}"
            )
        msg_id = hashlib.md5(str(raw_id).encode()).hexdigest()

        return {
            "message_id": msg_id,
            "chat_id": talker,
            "group_name": group_name,
            "sender_id": str(sender),
            "sender_name": str(sender_name),
            "content": resolved_content,
            "msg_type": int(msg.get("localType", msg.get("msg_type", 1))),
            "timestamp": ts,
            "is_at_mentioned": is_at,
            "is_group": True,
            "is_system_join": is_system_join,
            "new_member_id": new_member_id,
        }

    def _trim_dedup(self) -> None:
        """DedupSet handles this internally."""
        pass

    # ── Message sending ──────────────────────────────────────────────

    def _send_and_confirm(self, group_name: str, talker: str,
                          content: str) -> bool:
        """Send via WeChatWindowController (fire-and-forget).

        Returns True if the keyboard send action completed successfully.
        No confirmation polling — the window controller already retries
        on failure, and polling WCDB adds 3s of latency for marginal gain.
        """
        return self._window.send_to_chat(group_name, content)
