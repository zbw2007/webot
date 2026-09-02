from .dedup import Deduplicator


class ReadOnlyCollector:
    def __init__(self, fetch_page, storage, whitelist, page_size=50):
        self.fetch_page = fetch_page
        self.storage = storage
        self.whitelist = whitelist
        self.page_size = page_size
        self.cursor = (0, "")
        self.dedup = Deduplicator()

    def poll(self):
        while True:
            page = self.fetch_page(self.cursor, self.page_size)
            if not page:
                return self.cursor
            for message in page:
                position = (int(message["timestamp"]), str(message["message_id"]))
                if not self.whitelist.allows(message.get("chat_id"), message.get("is_group", False)):
                    self.cursor = max(self.cursor, position)
                    continue
                if self.dedup.accept(message):
                    self.storage.insert_message(message)
                self.cursor = max(self.cursor, position)
