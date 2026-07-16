"""One-record-per-search audit logging with recursive secret redaction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomic import atomic_write_bytes

_SECRET_KEYS = {"api_key", "crtfc_key", "cookie", "cookies", "authorization", "document_text", "raw_document"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _SECRET_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return [redact(v) for v in value]
    return value


class AuditLog:
    def __init__(self, path: Path, enabled: bool = True):
        self.path = path
        self.enabled = enabled

    def append_summary(self, record: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        try:
            existing = self.path.read_bytes() if self.path.exists() else b""
            line = json.dumps(redact(record), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            atomic_write_bytes(self.path, existing + line)
            return True
        except OSError:
            return False
