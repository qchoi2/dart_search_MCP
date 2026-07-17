"""Short opaque continuation tokens backed by bounded in-memory state."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.config.defaults import (
    CONTINUATION_MAX_ENTRIES,
    CONTINUATION_TOKEN_MAX_LENGTH,
    CONTINUATION_TOKEN_RANDOM_BYTES,
    CONTINUATION_TTL_SECONDS,
)
from app.errors import ErrorCode, SearchError


@dataclass(slots=True)
class _Entry:
    issued_at: float
    expires_at: float
    state: dict[str, Any]


class ContinuationStore:
    def __init__(
        self,
        ttl_seconds: int = CONTINUATION_TTL_SECONDS,
        *,
        max_entries: int = CONTINUATION_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ):
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("continuation ttl_seconds and max_entries must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.clock = clock
        self._entries: dict[str, _Entry] = {}

    def issue(self, state: dict[str, Any]) -> str:
        self.cleanup()
        while len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda key: self._entries[key].issued_at)
            self._entries.pop(oldest, None)
        token = "cursor_" + secrets.token_urlsafe(CONTINUATION_TOKEN_RANDOM_BYTES)
        now = self.clock()
        self._entries[token] = _Entry(now, now + self.ttl_seconds, state.copy())
        return token

    def consume(self, token: str, *, delete: bool = False) -> dict[str, Any]:
        if not token or len(token) > CONTINUATION_TOKEN_MAX_LENGTH or not token.startswith("cursor_"):
            raise SearchError(ErrorCode.INVALID_CONTINUATION_TOKEN, "continuation token 형식이 올바르지 않습니다.")
        entry = self._entries.get(token)
        if entry is None or entry.expires_at <= self.clock():
            self._entries.pop(token, None)
            raise SearchError(ErrorCode.INVALID_CONTINUATION_TOKEN, "continuation 세션이 만료되어 재검색이 필요합니다.")
        if delete:
            self._entries.pop(token, None)
        return entry.state.copy()

    def discard(self, token: str) -> None:
        self._entries.pop(token, None)

    def cleanup(self) -> None:
        now = self.clock()
        for token in [key for key, value in self._entries.items() if value.expires_at <= now]:
            self._entries.pop(token, None)
