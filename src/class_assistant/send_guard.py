import hashlib


class SendBlocked(ValueError):
    pass


class SendGuard:
    def __init__(self, real_send_enabled=False, dry_run=True):
        self.real_send_enabled = real_send_enabled
        self.dry_run = dry_run

    def check(self, draft, target_chat_id, allowed_chat_ids, sent_fingerprints=()):
        if target_chat_id not in set(allowed_chat_ids):
            raise SendBlocked("target chat is not whitelisted")
        if draft.get("status") != "approved":
            raise SendBlocked("draft is not approved")
        if draft.get("version") != draft.get("approved_version"):
            raise SendBlocked("draft version is not approved")
        if draft.get("chat_id") != target_chat_id:
            raise SendBlocked("target chat does not match draft")
        fingerprint = draft.get("send_fingerprint") or hashlib.sha256(draft.get("text", "").encode()).hexdigest()
        if fingerprint in sent_fingerprints:
            raise SendBlocked("duplicate send fingerprint")
        return True
