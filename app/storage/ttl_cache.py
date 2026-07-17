"""Short-lived, bounded disk cache for normalized OpenDART document text.

The cache is keyed only by a validated receipt number.  It never stores a
query, API key, cookie, response header, or raw ZIP.  Entries are checksummed
and corrupt/expired entries are treated as misses and removed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable

from app.config.defaults import RECEIPT_NO_LENGTH, TTL_DISK_HOURS, TTL_DISK_MAX_SIZE_MB
from app.storage.atomic import atomic_write_bytes
from app.storage.session_cache import SessionTextCache


class DiskTtlTextCache:
    def __init__(
        self,
        root: Path,
        *,
        ttl_hours: int = TTL_DISK_HOURS,
        max_size_mb: int = TTL_DISK_MAX_SIZE_MB,
        compression: str = "gzip1",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if compression not in {"none", "gzip1"}:
            raise ValueError("compression must be none or gzip1")
        if ttl_hours <= 0 or max_size_mb <= 0:
            raise ValueError("ttl_hours and max_size_mb must be positive")
        self.root = root
        self.ttl_seconds = ttl_hours * 3600
        self.max_bytes = max_size_mb * 1024 * 1024
        self.compression = compression
        self.clock = clock
        self.hits = 0
        self.misses = 0
        self.corruptions = 0

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or len(key) != RECEIPT_NO_LENGTH or not key.isdigit():
            raise ValueError(f"cache key must be a {RECEIPT_NO_LENGTH}-digit receipt number")

    def _path(self, key: str) -> Path:
        self._validate_key(key)
        digest = hashlib.sha256(key.encode("ascii")).hexdigest()
        suffix = ".json.gz" if self.compression == "gzip1" else ".json"
        return self.root / digest[:2] / f"{digest}{suffix}"

    def get(self, key: str) -> str | None:
        path = self._path(key)
        try:
            stat = path.stat()
        except FileNotFoundError:
            self.misses += 1
            return None
        if self.clock() - stat.st_mtime >= self.ttl_seconds:
            self._discard(path)
            self.misses += 1
            return None
        try:
            payload = path.read_bytes()
            if self.compression == "gzip1":
                payload = gzip.decompress(payload)
            record = json.loads(payload.decode("utf-8"))
            text = record["text"]
            if record.get("schema_version") != 1 or record.get("receipt_no") != key or not isinstance(text, str):
                raise ValueError("invalid cache record")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest != record.get("text_sha256"):
                raise ValueError("cache checksum mismatch")
        except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError, gzip.BadGzipFile):
            self.corruptions += 1
            self._discard(path)
            self.misses += 1
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        self.hits += 1
        return text

    def put(self, key: str, text: str) -> None:
        path = self._path(key)
        if not isinstance(text, str):
            raise ValueError("cache text must be a string")
        record = {
            "schema_version": 1,
            "receipt_no": key,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text": text,
        }
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.compression == "gzip1":
            payload = gzip.compress(payload, compresslevel=1, mtime=0)
        if len(payload) > self.max_bytes:
            return
        atomic_write_bytes(path, payload)
        try:
            timestamp = self.clock()
            os.utime(path, (timestamp, timestamp))
        except OSError:
            pass
        self.cleanup()

    def cleanup(self) -> dict[str, int]:
        if not self.root.exists():
            return {"removed_expired": 0, "removed_lru": 0, "remaining_bytes": 0}
        now = self.clock()
        files: list[tuple[Path, os.stat_result]] = []
        removed_expired = 0
        for path in self.root.rglob("*.json*"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if now - stat.st_mtime >= self.ttl_seconds:
                self._discard(path)
                removed_expired += 1
            else:
                files.append((path, stat))
        total = sum(stat.st_size for _, stat in files)
        removed_lru = 0
        for path, stat in sorted(files, key=lambda item: item[1].st_mtime):
            if total <= self.max_bytes:
                break
            self._discard(path)
            total -= stat.st_size
            removed_lru += 1
        return {"removed_expired": removed_expired, "removed_lru": removed_lru, "remaining_bytes": max(0, total)}

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class TieredTextCache:
    """Session LRU first, optional short-lived disk cache second."""

    def __init__(self, session: SessionTextCache | None = None, disk: DiskTtlTextCache | None = None) -> None:
        self.session = session or SessionTextCache()
        self.disk = disk
        self.hits = 0
        self.disk_hits = 0

    def get(self, key: str) -> str | None:
        text = self.session.get(key)
        if text is not None:
            self.hits += 1
            return text
        if self.disk is None:
            return None
        text = self.disk.get(key)
        if text is not None:
            self.disk_hits += 1
            self.hits += 1
            self.session.put(key, text)
        return text

    def get_session(self, key: str) -> str | None:
        text = self.session.get(key)
        if text is not None:
            self.hits += 1
        return text

    def put(self, key: str, text: str) -> None:
        self.session.put(key, text)
        if self.disk is not None:
            self.disk.put(key, text)

    def put_session(self, key: str, text: str) -> None:
        self.session.put(key, text)

    def __len__(self) -> int:
        return len(self.session)
