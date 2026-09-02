import hashlib
import json

from .models import require_message


def message_fingerprint(message):
    require_message(message)
    value = json.dumps([message["chat_id"], message["timestamp"], message["content"]], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Deduplicator:
    def __init__(self):
        self._ids = set()
        self._fingerprints = set()

    def accept(self, message):
        require_message(message)
        key = (message["chat_id"], str(message["message_id"]))
        fingerprint = message_fingerprint(message)
        if key in self._ids or fingerprint in self._fingerprints:
            return False
        self._ids.add(key)
        self._fingerprints.add(fingerprint)
        return True

