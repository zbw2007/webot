"""Application service for the read-only class-transaction assistant.

The service deliberately separates collection from analysis and sending.  The
WeChat backend can call :meth:`handle` for every message; that method never
returns a reply, so the existing backend cannot accidentally send an AI reply.
Analysis creates reviewable records and sending is available only through the
explicit approval/version checks below.
"""

from __future__ import annotations

import json
import hashlib
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from .analyzer import AnalysisError, analyze
from .models import require_message
from .safe_sender import SafeSender
from .scheduler import catch_up_slot, scheduled_slot
from .send_guard import SendBlocked, SendGuard
from .storage import Storage
from .whitelist import GroupWhitelist

logger = logging.getLogger(__name__)


class ClassAssistantService:
    """Coordinate whitelist collection, digest analysis and review-gated send."""

    def __init__(
        self,
        config: Any,
        *,
        storage: Storage | None = None,
        model_call: Callable[[list[dict[str, Any]]], Any] | None = None,
        sender: Callable[[str, str], bool] | None = None,
        window_validator: Callable[[str, str], bool] | None = None,
        summarizer: Any | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self._storage = storage or Storage(getattr(config, "db_path", "data/messages.db"))
        configured_groups = getattr(config, "class_assistant_groups", []) or []
        if isinstance(configured_groups, str):
            configured_groups = [part.strip() for part in configured_groups.split(",") if part.strip()]
        self._whitelist = GroupWhitelist(configured_groups)
        for chat_id in self._whitelist.chat_ids:
            self._storage.conn.execute(
                "INSERT OR IGNORE INTO group_whitelist(chat_id,display_name,enabled) VALUES(?,?,1)",
                (chat_id, ""),
            )
        self._storage.conn.commit()
        self._model_call = model_call
        self._sender = sender or (lambda _chat_id, _text: False)
        self._window_validator = window_validator
        self._clock = now or time.time
        self._summarizer = summarizer
        self._running = False
        self._thread: threading.Thread | None = None
        self._messages_processed = 0
        self._last_error: str | None = None
        self._emergency_stopped = False
        self._send_guard = SendGuard(
            real_send_enabled=bool(getattr(config, "class_assistant_real_send_enabled", False)),
            dry_run=bool(getattr(config, "class_assistant_dry_run", True)),
        )
        self._safe_sender = SafeSender(self._sender, self._send_guard)

    def set_sender(self, sender: Callable[[str, str], bool]) -> None:
        """Attach the backend sender after the backend has been constructed."""
        self._sender = sender
        self._safe_sender.send_callable = sender

    def set_window_validator(self, validator: Callable[[str, str], bool] | None) -> None:
        """Attach a backend-owned current-window/target validator."""
        self._window_validator = validator

    @property
    def whitelist(self) -> GroupWhitelist:
        return self._whitelist

    @property
    def storage(self) -> Storage:
        return self._storage

    def _enabled(self, kind: str) -> bool:
        if not bool(getattr(self.config, "class_assistant_enabled", False)):
            return False
        if kind == "collection":
            return bool(
                getattr(
                    self.config,
                    "class_assistant_collection_enabled",
                    getattr(self.config, "class_assistant_collect_enabled", False),
                )
            )
        if kind == "analysis":
            return bool(
                getattr(
                    self.config,
                    "class_assistant_analysis_enabled",
                    getattr(self.config, "class_assistant_analyze_enabled", False),
                )
            )
        return False

    def handle(self, message: Mapping[str, Any]) -> None:
        """Collect one message if it is an enabled, whitelisted group message.

        This callback *always* returns ``None``.  That is a safety boundary for
        ``WcdbBackend``, whose callback return value is otherwise sent directly
        to WeChat.
        """
        if self._emergency_stopped or not self._enabled("collection"):
            return None
        try:
            require_message(message)
        except ValueError:
            logger.warning("Ignoring malformed class-assistant message")
            return None
        if not self._whitelist.allows(message.get("chat_id"), bool(message.get("is_group", False))):
            return None
        try:
            expires_at = int(self._clock()) + int(getattr(self.config, "raw_message_retention_days", 7)) * 86400
            self._storage.insert_message(dict(message), expires_at=expires_at)
            self._messages_processed += 1
        except Exception:
            logger.exception("Failed to persist class-assistant message")
        return None

    def is_in_scope(self, message: Mapping[str, Any]) -> bool:
        """Return whether a message belongs to this assistant's collection scope."""
        return not self._emergency_stopped and self._enabled("collection") and self._whitelist.allows(
            message.get("chat_id"), bool(message.get("is_group", False))
        )

    def _default_model_call(self, messages: list[dict[str, Any]]) -> Any:
        """Call the configured summarizer using a strict JSON-only prompt."""
        if self._summarizer is None:
            raise RuntimeError("class-assistant model is not configured")
        prompt = (
            "你是班级事务助手。只返回 JSON，不要 Markdown。字段必须为："
            "summary(string), todos(array), reply_candidates(array)。"
            "todo 使用 title,due_at,due_confidence,source_message_id；"
            "reply_candidates 使用 text,risk_level,source_message_id。"
            "日期不明确时 due_confidence=needs_confirmation。"
        )
        content = "\n".join(
            f"[{item.get('timestamp')}] {item.get('sender_name', '')}: {item.get('content', '')}"
            for item in messages
        )
        raw = self._summarizer._call_chat_api(prompt, [{"role": "user", "content": content}])
        return raw

    def _call_model(self, messages: list[dict[str, Any]]) -> Any:
        if self._model_call is not None:
            return self._model_call(messages)
        return self._default_model_call(messages)

    def _ensure_summarizer(self) -> None:
        if self._summarizer is not None:
            return
        # Lazy import keeps read-only tests and collection usable without the
        # optional AI client packages installed.
        from src.summarize import create_summarizer

        self._summarizer = create_summarizer(self.config)

    def _completed_slots(self) -> list[str]:
        return [str(row["scheduled_slot"]) for row in self._storage.query("digest_runs", status="succeeded")]

    def _slot(self, value: datetime, force: bool) -> tuple[str | None, bool, bool]:
        zone = getattr(self.config, "timezone", "Asia/Shanghai")
        slot = scheduled_slot(value, zone)
        catch_up = False
        if slot is None:
            # Before today's 08:00 slot, the only eligible missed run is the
            # previous day's 20:00 slot.  It is attempted once by the unique
            # scheduled_slot key, then never replayed automatically.
            local = value
            if local.tzinfo is None:
                from zoneinfo import ZoneInfo

                local = local.replace(tzinfo=ZoneInfo(zone))
            local = local - timedelta(days=1)
            slot = local.replace(hour=20, minute=0, second=0, microsecond=0).isoformat(timespec="minutes")
            catch_up = True
        completed = set(self._completed_slots())
        return slot, bool(slot in completed if slot else False), catch_up

    def _messages_for_digest(self) -> list[dict[str, Any]]:
        rows = self._storage.query("captured_messages")
        allowed = self._whitelist.chat_ids
        return [
            dict(row)
            for row in sorted(rows, key=lambda row: (int(row["timestamp"]), str(row["message_id"])))
            if row["chat_id"] in allowed
            and (int(row["timestamp"]), str(row["message_id"])) > self._storage.get_analysis_cursor(row["chat_id"])
        ]

    @staticmethod
    def _source_id(item: Mapping[str, Any], messages: Iterable[Mapping[str, Any]]) -> str | None:
        message_list = list(messages)
        valid_ids = {str(message.get("message_id")) for message in message_list}
        source = item.get("source_message_id")
        if source and str(source) in valid_ids:
            return str(source)
        first = message_list[0] if message_list else None
        return str(first["message_id"]) if first else None

    @staticmethod
    def _risk(item: Mapping[str, Any]) -> str:
        risk = str(item.get("risk_level", "low")).lower()
        text = str(item.get("text", ""))
        if any(keyword in text for keyword in ("请假", "成绩", "费用", "承诺", "投诉", "隐私")):
            return "high"
        return risk if risk in {"low", "medium", "high"} else "medium"

    def run_digest(self, now: datetime | None = None, *, force: bool = False) -> dict[str, Any]:
        """Analyze the current digest slot and persist only validated output."""
        if self._emergency_stopped or not self._enabled("analysis"):
            return {"status": "disabled"}
        value = now or datetime.now().astimezone()
        slot, already_completed, is_catch_up = self._slot(value, force)
        if slot is None:
            return {"status": "not_due"}
        if already_completed and not force:
            return {"status": "skipped", "scheduled_slot": slot}

        run_id = uuid.uuid4().hex
        started = int(self._clock())
        self._storage.insert_digest_run({
            "run_id": run_id,
            "scheduled_slot": slot,
            "status": "running",
            "is_catch_up": int(is_catch_up),
            "started_at": started,
        })
        messages = self._messages_for_digest()
        try:
            if self._model_call is None:
                self._ensure_summarizer()
            by_group: dict[str, list[dict[str, Any]]] = {}
            for message in messages:
                by_group.setdefault(str(message["chat_id"]), []).append(message)
            window_start = window_end = None
            if messages:
                from zoneinfo import ZoneInfo

                zone = ZoneInfo(getattr(self.config, "timezone", "Asia/Shanghai"))
                timestamps = [int(message["timestamp"]) for message in messages]
                window_start = datetime.fromtimestamp(min(timestamps), zone).isoformat()
                window_end = datetime.fromtimestamp(max(timestamps), zone).isoformat()
            summaries: list[str] = []
            analyzed_groups: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
            for chat_id, group_messages in by_group.items():
                # Analyze each whitelisted group independently so a teacher's
                # notice never gets mixed with another class's context.
                result = analyze(group_messages, self._call_model)
                if result.get("summary"):
                    summaries.append(str(result["summary"]))
                analyzed_groups.append((chat_id, group_messages, result))

            # Build all rows before writing any of them.  A model/schema error
            # in a later group therefore cannot leave an earlier group partly
            # persisted or advance its cursor.
            todo_rows: list[dict[str, Any]] = []
            draft_rows: list[dict[str, Any]] = []
            for chat_id, group_messages, result in analyzed_groups:
                default_source = group_messages[0]["message_id"] if group_messages else None
                for todo in result.get("todos", []):
                    todo_rows.append({
                        "group_id": todo.get("group_id") or chat_id,
                        "title": todo["title"].strip(),
                        "description": todo.get("description", "").strip(),
                        "location": todo.get("location", "").strip(),
                        "assignee": todo.get("assignee", "").strip(),
                        "due_at": todo.get("due_at"),
                        "due_confidence": todo.get("due_confidence", "unknown"),
                        "status": "open",
                        "source_message_id": self._source_id(todo, group_messages) or default_source,
                        "created_at": started,
                    })
                for index, candidate in enumerate(result.get("reply_candidates", []), 1):
                    source_id = self._source_id(candidate, group_messages) or default_source
                    source_row = next((row for row in group_messages if row["message_id"] == source_id), group_messages[0] if group_messages else {})
                    draft_id = f"draft-{slot}-{chat_id}-{index}"
                    text = candidate["text"].strip()
                    draft_rows.append({
                        "id": draft_id,
                        "version": 1,
                        "chat_id": source_row.get("chat_id", chat_id),
                        "group_name": source_row.get("group_name", ""),
                        "text": text,
                        "status": "pending_review",
                        "risk_level": self._risk(candidate),
                        "send_fingerprint": self._fingerprint(source_row.get("chat_id", chat_id), text),
                        "source_message_id": source_id,
                        "created_at": started,
                        "expires_at": started + int(getattr(self.config, "draft_retention_days", 30)) * 86400,
                    })
            with self._storage.transaction():
                for todo in todo_rows:
                    self._storage.insert_todo(todo, commit=False)
                for draft in draft_rows:
                    self._storage.insert_reply_draft(draft, commit=False)
                # Advance analysis cursors only after every group's response
                # passed schema validation and all records are in the same
                # transaction as the successful digest marker.
                for chat_id, group_messages in by_group.items():
                    if group_messages:
                        last = max(group_messages, key=lambda item: (int(item["timestamp"]), str(item["message_id"])))
                        self._storage.advance_analysis_cursor(chat_id, last["timestamp"], last["message_id"], commit=False)
                self._storage.insert_digest_run({
                    "run_id": run_id,
                    "scheduled_slot": slot,
                    "status": "succeeded",
                    "is_catch_up": int(is_catch_up),
                    "window_start": window_start,
                    "window_end": window_end,
                    "started_at": started,
                    "completed_at": int(self._clock()),
                }, commit=False)
            self._last_error = None
            return {"status": "succeeded", "scheduled_slot": slot, "summary": "\n".join(summaries)}
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("Class-assistant digest failed")
            self._storage.insert_digest_run({
                "run_id": run_id,
                "scheduled_slot": slot,
                "status": "failed",
                "is_catch_up": int(is_catch_up),
                "window_start": locals().get("window_start"),
                "window_end": locals().get("window_end"),
                "started_at": started,
                "completed_at": int(self._clock()),
                "error": str(exc),
            })
            return {"status": "failed", "scheduled_slot": slot, "error": str(exc)}

    def _latest_draft(self, draft_id: str) -> dict[str, Any] | None:
        rows = self._storage.query("reply_drafts", id=draft_id)
        if not rows:
            return None
        return dict(max(rows, key=lambda row: int(row["version"])))

    def _write_draft_status(self, draft_id: str, version: int, status: str, approved_version: int | None = None) -> dict[str, Any]:
        row = self._storage.update_draft_status(draft_id, version, status, approved_version)
        if row is None:
            raise ValueError("draft not found")
        return row

    def approve_draft(self, draft_id: str, version: int, actor: str = "local") -> dict[str, Any]:
        draft = self._latest_draft(draft_id)
        if draft is None or int(draft["version"]) != int(version):
            raise ValueError("draft version is not the latest version")
        if draft["status"] not in {"pending_review", "edited"}:
            raise ValueError("draft is not awaiting review")
        if draft["risk_level"] == "high" and draft["status"] != "edited":
            raise ValueError("high-risk draft must be edited before approval")
        result = self._write_draft_status(draft_id, version, "approved", version)
        self._storage.insert_audit({"draft_id": draft_id, "action": "approved", "actor": actor, "details": f"version={version}"})
        return result

    def edit_draft(self, draft_id: str, text: str, actor: str = "local") -> dict[str, Any]:
        if not text or not text.strip():
            raise ValueError("draft text cannot be empty")
        draft = self._latest_draft(draft_id)
        if draft is None:
            raise ValueError("draft not found")
        if draft["status"] not in {"pending_review", "edited", "approved"}:
            raise ValueError("draft cannot be edited in its current state")
        version = int(draft["version"]) + 1
        result = self._storage.insert_reply_draft({
            "id": draft_id,
            "version": version,
            "chat_id": draft["chat_id"],
            "group_name": draft["group_name"],
            "text": text.strip(),
            "status": "edited",
            "risk_level": draft["risk_level"],
            "source_message_id": draft.get("source_message_id"),
            "created_at": int(self._clock()),
            "expires_at": draft["expires_at"],
        })
        self._storage.insert_audit({"draft_id": draft_id, "action": "edited", "actor": actor, "details": f"version={version}"})
        return dict(result)

    def reject_draft(self, draft_id: str, version: int, actor: str = "local") -> dict[str, Any]:
        draft = self._latest_draft(draft_id)
        if draft is None or int(draft["version"]) != int(version):
            raise ValueError("draft version is not the latest version")
        if draft["status"] not in {"pending_review", "edited", "approved"}:
            raise ValueError("draft cannot be rejected in its current state")
        result = self._write_draft_status(draft_id, version, "rejected", None)
        self._storage.insert_audit({"draft_id": draft_id, "action": "rejected", "actor": actor, "details": f"version={version}"})
        return result

    def send_draft(
        self,
        draft_id: str,
        *,
        version: int | None = None,
        confirmation_token: str | None = None,
        target_group_name: str | None = None,
        current_window: str | None = None,
    ) -> dict[str, Any]:
        draft = self._latest_draft(draft_id)
        if draft is None:
            raise ValueError("draft not found")
        if version is not None and int(draft["version"]) != int(version):
            raise ValueError("draft version is not the latest version")
        fingerprint = draft.get("send_fingerprint") or self._fingerprint(draft.get("chat_id", ""), draft.get("text", ""))
        if not draft.get("send_fingerprint"):
            self._storage.set_draft_fingerprint(draft_id, draft["version"], fingerprint)
            draft["send_fingerprint"] = fingerprint

        def _before_send():
            if self._window_validator is None:
                raise SendBlocked("backend window validator is not configured")
            if not self._window_validator(str(draft["chat_id"]), str(draft.get("group_name", ""))):
                raise SendBlocked("current WeChat window does not match target group")
            return self._storage.claim_draft_for_sending(draft_id, int(draft["version"]))

        try:
            payload = self._safe_sender.send(
                draft,
                draft["chat_id"],
                self._whitelist.chat_ids,
                sent_fingerprints=self._sent_fingerprints(),
                target_group_name=target_group_name or draft.get("group_name"),
                current_window=current_window,
                confirmation_token=confirmation_token,
                before_send=_before_send,
            )
        except Exception as exc:
            self._storage.insert_audit({"draft_id": draft_id, "action": "send_failed", "actor": "local", "details": json.dumps({"version": int(draft["version"]), "error": str(exc)})})
            raise
        if payload.get("dry_run"):
            self._storage.insert_audit({"draft_id": draft_id, "action": "send_dry_run", "actor": "local", "details": f"version={draft['version']}"})
            return payload
        if payload.get("sent"):
            self._write_draft_status(draft_id, int(draft["version"]), "sent", int(draft["version"]))
            action = "sent"
        else:
            self._write_draft_status(draft_id, int(draft["version"]), "needs_reconciliation", int(draft["version"]))
            action = "send_unconfirmed"
        self._storage.insert_audit({"draft_id": draft_id, "action": action, "actor": "local", "details": json.dumps({"version": int(draft["version"]), "fingerprint": fingerprint})})
        return payload

    @staticmethod
    def _fingerprint(chat_id: str, text: str) -> str:
        return hashlib.sha256(f"{chat_id}\0{text}".encode("utf-8")).hexdigest()

    def _sent_fingerprints(self) -> set[str]:
        rows = self._storage.query("audit_events", action="sent")
        fingerprints = set()
        for row in rows:
            try:
                value = json.loads(row["details"])
                if value.get("fingerprint"):
                    fingerprints.add(str(value["fingerprint"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return fingerprints

    def issue_confirmation_token(self) -> str:
        return self._send_guard.issue_confirmation_token()

    def list_records(self, table: str, **filters: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in self._storage.query(table, **filters)]

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled("collection") or self._enabled("analysis"),
            "collection_enabled": self._enabled("collection"),
            "analysis_enabled": self._enabled("analysis"),
            "review_queue_enabled": bool(getattr(self.config, "class_assistant_review_queue_enabled", True)),
            "real_send_enabled": bool(getattr(self.config, "class_assistant_real_send_enabled", False)),
            "dry_run": self._send_guard.dry_run,
            "groups": sorted(self._whitelist.chat_ids),
            "messages_processed": self._messages_processed,
            "running": self._running,
            "emergency_stopped": self._emergency_stopped,
            "last_error": self._last_error,
        }

    def _loop(self) -> None:
        while self._running:
            try:
                now = datetime.now().astimezone()
                self.run_digest(now)
                self._storage.cleanup(
                    now=int(self._clock()),
                    raw_days=int(getattr(self.config, "raw_message_retention_days", 7)),
                    draft_days=int(getattr(self.config, "draft_retention_days", 30)),
                    audit_days=int(getattr(self.config, "audit_retention_days", 30)),
                )
            except Exception:
                logger.exception("Class-assistant scheduler cycle failed")
            # A minute-level poll is sufficient; scheduled_slot makes runs
            # idempotent and the daemon remains responsive to stop().
            self._stop_event.wait(60)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="class-assistant", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        event = getattr(self, "_stop_event", None)
        if event is not None:
            event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def emergency_stop(self) -> None:
        """Stop scheduling and permanently block collection for this process."""
        self._emergency_stopped = True
        self.stop()

    def reconcile_draft(self, draft_id: str, version: int, outcome: str, actor: str = "local") -> dict[str, Any]:
        """Resolve a crashed/uncertain send without automatically retrying."""
        draft = self._latest_draft(draft_id)
        if draft is None or int(draft["version"]) != int(version):
            raise ValueError("draft version is not the latest version")
        if draft["status"] != "sending":
            raise ValueError("only a sending draft can be reconciled")
        if outcome == "sent":
            status = "sent"
            action = "reconciled_sent"
        elif outcome in {"failed", "unsent"}:
            status = "needs_reconciliation"
            action = "reconciled_failed"
        else:
            raise ValueError("outcome must be sent or failed")
        result = self._write_draft_status(draft_id, version, status, version)
        self._storage.insert_audit({"draft_id": draft_id, "action": action, "actor": actor, "details": f"version={version}"})
        return result

    def close(self) -> None:
        """Stop scheduling and release the assistant's dedicated DB handle."""
        self.stop()
        self._storage.close()
