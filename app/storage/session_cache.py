"""Byte-accounted LRU parsed-text session cache."""

from __future__ import annotations

from collections import OrderedDict

from app.config.defaults import SESSION_CACHE_MAX_DOCUMENTS, SESSION_CACHE_MAX_TEXT_MB


class SessionTextCache:
    def __init__(self, max_documents: int = SESSION_CACHE_MAX_DOCUMENTS, max_text_mb: int = SESSION_CACHE_MAX_TEXT_MB):
        self.max_documents = max_documents
        self.max_bytes = max_text_mb * 1024 * 1024
        self._items: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self.total_bytes = 0
        self.hits = 0

    def get(self, key: str) -> str | None:
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return item[0]

    def put(self, key: str, text: str) -> None:
        size = len(text.encode("utf-8"))
        if size > self.max_bytes:
            return
        old = self._items.pop(key, None)
        if old:
            self.total_bytes -= old[1]
        self._items[key] = (text, size)
        self.total_bytes += size
        while len(self._items) > self.max_documents or self.total_bytes > self.max_bytes:
            _, (_, evicted_size) = self._items.popitem(last=False)
            self.total_bytes -= evicted_size

    def __len__(self) -> int:
        return len(self._items)
