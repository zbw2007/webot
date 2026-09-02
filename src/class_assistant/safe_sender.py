from .send_guard import SendGuard


class SafeSender:
    """Approval-gated sender; the injected callable is never called in dry-run."""

    def __init__(self, send_callable, guard=None):
        self.send_callable = send_callable
        self.guard = guard or SendGuard()

    def send(self, draft, target_chat_id, allowed_chat_ids, sent_fingerprints=()):
        self.guard.check(draft, target_chat_id, allowed_chat_ids, sent_fingerprints)
        if self.guard.dry_run or not self.guard.real_send_enabled:
            return {"sent": False, "dry_run": True}
        ok = bool(self.send_callable(target_chat_id, draft["text"]))
        return {"sent": ok, "dry_run": False}
