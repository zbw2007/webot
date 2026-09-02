class GroupWhitelist:
    def __init__(self, chat_ids):
        if chat_ids is None:
            raise ValueError("group whitelist cannot be empty")
        raw = [str(x).strip() for x in chat_ids]
        if any(not x for x in raw):
            raise ValueError("group whitelist values must be non-empty")
        ids = set(raw)
        if "*" in ids:
            raise ValueError("wildcard group whitelist is not allowed")
        self._chat_ids = frozenset(ids)

    def allows(self, chat_id, is_group=True):
        return bool(is_group and chat_id and chat_id in self._chat_ids)

    @property
    def chat_ids(self):
        return self._chat_ids
