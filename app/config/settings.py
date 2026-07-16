"""Validated settings loader with safe recovery for damaged JSON."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.storage.atomic import atomic_write_json

from .defaults import DEFAULT_SETTINGS, INTERACTIVE_TARGET_MAX


def _merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    values: dict[str, Any]
    source: Path | None = None
    recovered_from_error: bool = False

    def get(self, dotted: str, default: Any = None) -> Any:
        value: Any = self.values
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def validate_settings(values: dict[str, Any]) -> None:
    _positive_int(values["search"]["list_concurrency"], "search.list_concurrency")
    _positive_int(values["search"]["document_concurrency"], "search.document_concurrency")
    _positive_int(values["search"]["interactive_document_budget"], "search.interactive_document_budget")
    _positive_int(values["search"]["max_results"], "search.max_results")
    if values["search"]["max_results"] > INTERACTIVE_TARGET_MAX:
        raise ValueError(f"search.max_results cannot exceed {INTERACTIVE_TARGET_MAX}")
    if not isinstance(values["cache"]["ttl_disk_enabled"], bool):
        raise ValueError("cache.ttl_disk_enabled must be boolean")


def load_settings(path: Path | None = None) -> Settings:
    if path is None or not path.exists():
        return Settings(copy.deepcopy(DEFAULT_SETTINGS), path)
    recovered = False
    try:
        user = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(user, dict):
            raise ValueError("settings root must be an object")
        values = _merge(DEFAULT_SETTINGS, user)
        validate_settings(values)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        recovered = True
        backup = path.with_suffix(path.suffix + ".invalid")
        try:
            backup.write_bytes(path.read_bytes())
            atomic_write_json(path, DEFAULT_SETTINGS)
        except OSError:
            pass
        values = copy.deepcopy(DEFAULT_SETTINGS)
    return Settings(values, path, recovered)
