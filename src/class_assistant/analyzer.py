import json
from typing import Literal

try:  # Optional at import time so read-only collection works without clients.
    from pydantic import BaseModel, ConfigDict, Field
except ImportError:  # pragma: no cover - exercised only in minimal installs
    BaseModel = None


if BaseModel is not None:
    class _TodoPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str
        due_at: str | None = None
        due_confidence: Literal["high", "medium", "low", "unknown", "needs_confirmation"] = "unknown"
        group_id: str | None = None
        source_message_id: str | None = None

    class _ReplyPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str
        risk_level: Literal["low", "medium", "high"] = "low"
        source_message_id: str | None = None

    class _AnalysisPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        summary: str = ""
        todos: list[_TodoPayload] = Field(default_factory=list)
        reply_candidates: list[_ReplyPayload] = Field(default_factory=list)


class AnalysisError(ValueError):
    pass


def analyze(messages, call_model):
    payload = call_model(messages)
    try:
        result = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, json.JSONDecodeError) as exc:
        raise AnalysisError("model returned invalid JSON") from exc
    if not isinstance(result, dict) or set(result) - {"todos", "reply_candidates", "summary"}:
        raise AnalysisError("analysis schema invalid")
    if BaseModel is not None:
        try:
            return _AnalysisPayload.model_validate(result).model_dump(exclude_none=True)
        except Exception as exc:
            raise AnalysisError("analysis schema invalid") from exc
    if not isinstance(result.get("todos"), list) or not isinstance(result.get("reply_candidates"), list):
        raise AnalysisError("analysis schema invalid")
    for todo in result["todos"]:
        if not isinstance(todo, dict) or not isinstance(todo.get("title"), str) or not todo["title"].strip():
            raise AnalysisError("todo schema invalid")
        if todo.get("due_confidence", "unknown") not in {"high", "medium", "low", "unknown", "needs_confirmation"}:
            raise AnalysisError("todo due confidence invalid")
    for candidate in result["reply_candidates"]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("text"), str) or not candidate["text"].strip():
            raise AnalysisError("reply candidate schema invalid")
        if candidate.get("risk_level", "low") not in {"low", "medium", "high"}:
            raise AnalysisError("reply risk level invalid")
    return result


def analyze_without_advancing(messages, call_model, state=None):
    """Analyze strictly; caller state is changed only after a valid result."""
    result = analyze(messages, call_model)
    if state is not None:
        state["last_result"] = result
    return result
