from dataclasses import dataclass
from typing import Any, Mapping


def require_message(message: Mapping[str, Any]) -> None:
    required = ("message_id", "chat_id", "content", "timestamp")
    missing = [key for key in required if message.get(key) in (None, "")]
    if missing:
        raise ValueError("missing message fields: " + ", ".join(missing))


@dataclass(frozen=True)
class Todo:
    title: str
    source_message_ids: tuple[str, ...] = ()
    due_at: str | None = None
    due_confidence: str = "unknown"

    def __post_init__(self):
        if not self.title.strip():
            raise ValueError("todo title cannot be empty")
        if self.due_confidence not in {"high", "medium", "low", "unknown"}:
            raise ValueError("invalid due confidence")


@dataclass(frozen=True)
class ReplyDraft:
    draft_id: str
    chat_id: str
    text: str
    version: int = 1
    status: str = "pending_review"
    risk_level: str = "low"

    def __post_init__(self):
        if not self.draft_id or not self.chat_id or not self.text.strip():
            raise ValueError("draft id, chat id and text are required")
        if self.version < 1:
            raise ValueError("draft version must be positive")

