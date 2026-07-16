"""One-record-per-search audit logging with recursive secret redaction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config.defaults import AUDIT_MAX_SIZE_MB

from .atomic import atomic_write_bytes

_SECRET_KEYS = {"api_key", "crtfc_key", "cookie", "cookies", "authorization", "document_text", "raw_document"}


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


class AuditLog:
    def __init__(self, path: Path, enabled: bool = True, max_size_mb: int = AUDIT_MAX_SIZE_MB):
        self.path = path
        self.enabled = enabled
        self.max_bytes = max_size_mb * 1024 * 1024

    def append_summary(self, record: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        try:
            existing = self.path.read_bytes() if self.path.exists() else b""
            line = json.dumps(redact(record), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            combined = existing + line
            if len(combined) > self.max_bytes:
                combined = combined[-self.max_bytes :]
                first_newline = combined.find(b"\n")
                combined = combined[first_newline + 1 :] if first_newline >= 0 else line[-self.max_bytes :]
            atomic_write_bytes(self.path, combined)
            return True
        except OSError:
            return False
