TRANSITIONS = {
    ("pending_review", "edit"): "edited",
    ("pending_review", "approve"): "approved",
    ("edited", "approve"): "approved",
    ("approved", "send"): "sending",
    ("sending", "success"): "sent",
    ("pending_review", "reject"): "rejected",
    ("edited", "reject"): "rejected",
    ("approved", "edit"): "edited",
}


def transition(status, action):
    try:
        return TRANSITIONS[(status, action)]
    except KeyError as exc:
        raise ValueError(f"invalid draft transition: {status} + {action}") from exc

