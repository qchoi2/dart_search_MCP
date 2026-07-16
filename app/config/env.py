"""Minimal .env loader that never logs secret values."""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path, *, override: bool = False) -> set[str]:
    loaded: set[str] = set()
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value
        loaded.add(key)
    return loaded


def get_opendart_api_key() -> str | None:
    value = os.environ.get("DART_API_KEY") or os.environ.get("OPENDART_API_KEY")
    return value.strip() if value and value.strip() else None
