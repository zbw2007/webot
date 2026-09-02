from .send_guard import SendGuard
from .send_guard import SendBlocked


class SafeSender:
    """Approval-gated sender; the injected callable is never called in dry-run."""

    def __init__(self, send_callable, guard=None):
        self.send_callable = send_callable
        self.guard = guard or SendGuard()

    def send(self, draft, target_chat_id, allowed_chat_ids, sent_fingerprints=(), *, before_send=None, after_send=None, **kwargs):
        self.guard.check(draft, target_chat_id, allowed_chat_ids, sent_fingerprints, **kwargs)
        if self.guard.dry_run or not self.guard.real_send_enabled:
            return {"sent": False, "dry_run": True}
        if before_send:
            claimed = before_send()
            if claimed is False:
                raise SendBlocked("draft was already claimed for sending")
        # If the callable raises, the draft remains in ``sending`` and is not
        # retried automatically; the operator can reconcile it explicitly.
        ok = bool(self.send_callable(target_chat_id, draft["text"]))
        if after_send:
            after_send(ok)
        return {"sent": ok, "dry_run": False}
