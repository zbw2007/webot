import hashlib
import secrets


class SendBlocked(ValueError):
    pass


class SendGuard:
    def __init__(self, real_send_enabled=False, dry_run=True):
        self.real_send_enabled = real_send_enabled
        self.dry_run = dry_run
        self._confirmation_tokens = set()

    def issue_confirmation_token(self):
        token = secrets.token_urlsafe(24)
        self._confirmation_tokens.add(token)
        return token

    def check(self, draft, target_chat_id, allowed_chat_ids, sent_fingerprints=(), *, target_group_name=None, current_window=None, expected_window=None, confirmation_token=None):
        if target_chat_id not in set(allowed_chat_ids):
            raise SendBlocked("target chat is not whitelisted")
        if draft.get("status") != "approved":
            raise SendBlocked("draft is not approved")
        if draft.get("version") != draft.get("approved_version"):
            raise SendBlocked("draft version is not approved")
        if draft.get("chat_id") != target_chat_id:
            raise SendBlocked("target chat does not match draft")
        if target_group_name is not None and draft.get("group_name") != target_group_name:
            raise SendBlocked("target group name does not match draft")
        if expected_window is not None and current_window != expected_window:
            raise SendBlocked("current window does not match target")
        if not self.dry_run:
            if confirmation_token not in self._confirmation_tokens:
                raise SendBlocked("confirmation token is required")
            self._confirmation_tokens.remove(confirmation_token)
        fingerprint = draft.get("send_fingerprint") or hashlib.sha256(
            f"{target_chat_id}\0{draft.get('text', '')}".encode("utf-8")
        ).hexdigest()
        if fingerprint in sent_fingerprints:
            raise SendBlocked("duplicate send fingerprint")
        return True

    def send(self, draft, target_chat_id, allowed_chat_ids, sender, sent_fingerprints=(), **kwargs):
        self.check(draft, target_chat_id, allowed_chat_ids, sent_fingerprints, **kwargs)
        if self.dry_run or not self.real_send_enabled:
            return {"dry_run": True, "chat_id": target_chat_id, "text": draft.get("text", "")}
        return sender(target_chat_id, draft.get("text", ""))
