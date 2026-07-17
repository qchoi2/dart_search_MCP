"""One-record-per-search audit logging with recursive secret redaction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config.defaults import AUDIT_MAX_SIZE_MB

from .atomic import atomic_write_bytes

_SECRET_KEYS = {"api_key", "crtfc_key", "cookie", "cookies", "authorization", "document_text", "raw_document"}
_QUERY_TEXT_KEYS = {"query", "original_query", "raw_query", "query_text", "user_query"}


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith("_api_key") or "cookie" in normalized or "authorization" in normalized


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if _is_secret_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return [redact(v) for v in value]
    return value


def minimize_query_text(value: Any, *, allow_query_text: bool) -> Any:
    if isinstance(value, dict):
        return {
            key: minimize_query_text(item, allow_query_text=allow_query_text)
            for key, item in value.items()
            if allow_query_text or key.lower().replace("-", "_") not in _QUERY_TEXT_KEYS
        }
    if isinstance(value, list):
        return [minimize_query_text(item, allow_query_text=allow_query_text) for item in value]
    if isinstance(value, tuple):
        return [minimize_query_text(item, allow_query_text=allow_query_text) for item in value]
    return value


class AuditLog:
    def __init__(
        self,
        path: Path,
        enabled: bool = True,
        max_size_mb: int = AUDIT_MAX_SIZE_MB,
        *,
        audit_query_text: bool = False,
    ):
        self.path = path
        self.enabled = enabled
        self.max_bytes = max_size_mb * 1024 * 1024
        self.audit_query_text = audit_query_text

    def append_summary(self, record: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        try:
            existing = self.path.read_bytes() if self.path.exists() else b""
            minimized = minimize_query_text(record, allow_query_text=self.audit_query_text)
            line = json.dumps(redact(minimized), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            combined = existing + line
            if len(combined) > self.max_bytes:
                combined = combined[-self.max_bytes :]
                first_newline = combined.find(b"\n")
                combined = combined[first_newline + 1 :] if first_newline >= 0 else line[-self.max_bytes :]
            atomic_write_bytes(self.path, combined)
            return True
        except OSError:
            return False
