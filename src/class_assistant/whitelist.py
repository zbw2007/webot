def is_auto_discovery_token(value) -> bool:
    """Whether a value requests legacy auto-discovery semantics."""
    return isinstance(value, str) and value.strip().casefold() in {"*", "all"}


class GroupWhitelist:
    def __init__(self, chat_ids):
        if chat_ids is None:
            raise ValueError("group whitelist cannot be empty")
        if isinstance(chat_ids, str):
            chat_ids = [chat_ids]
        try:
            raw = list(chat_ids)
        except TypeError:
            raise ValueError("group whitelist must be iterable") from None
        if any(not isinstance(x, str) for x in raw):
            raise ValueError("group whitelist values must be strings")
        raw = [x.strip() for x in raw]
        if any(not x for x in raw):
            raise ValueError("group whitelist values must be non-empty")
        ids = set(raw)
        if any(is_auto_discovery_token(x) for x in ids):
            raise ValueError("wildcard group whitelist is not allowed")
        if not ids:
            raise ValueError("group whitelist cannot be empty")
        self._chat_ids = frozenset(ids)

    def allows(self, chat_id, is_group=True):
        return bool(is_group and chat_id and chat_id in self._chat_ids)

    @property
    def chat_ids(self):
        return self._chat_ids
