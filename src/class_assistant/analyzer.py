import json


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
    if not isinstance(result.get("todos"), list) or not isinstance(result.get("reply_candidates"), list):
        raise AnalysisError("analysis schema invalid")
    for todo in result["todos"]:
        if not isinstance(todo, dict) or not isinstance(todo.get("title"), str) or not todo["title"].strip():
            raise AnalysisError("todo schema invalid")
    for candidate in result["reply_candidates"]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("text"), str) or not candidate["text"].strip():
            raise AnalysisError("reply candidate schema invalid")
    return result


def analyze_without_advancing(messages, call_model, state=None):
    """Analyze strictly; caller state is changed only after a valid result."""
    result = analyze(messages, call_model)
    if state is not None:
        state["last_result"] = result
    return result
