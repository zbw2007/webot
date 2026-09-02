import json


class AnalysisError(ValueError):
    pass


def analyze(messages, call_model):
    payload = call_model(messages)
    try:
        result = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, json.JSONDecodeError) as exc:
        raise AnalysisError("model returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("todos", []), list) or not isinstance(result.get("reply_candidates", []), list):
        raise AnalysisError("analysis schema invalid")
    return result

