class GroupWhitelist:
    def __init__(self, chat_ids):
        ids = {str(x).strip() for x in chat_ids if str(x).strip()}
        if "*" in ids:
            ids.remove("*")
        self._chat_ids = frozenset(ids)

    def allows(self, chat_id, is_group=True):
        return bool(is_group and chat_id and chat_id in self._chat_ids)

    @property
    def chat_ids(self):
        return self._chat_ids

