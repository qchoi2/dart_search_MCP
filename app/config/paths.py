"""Local data paths; no directory is created until it is needed."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    local_data: Path
    cache: Path
    logs: Path
    temp: Path
    usage: Path

    @classmethod
    def discover(cls, root: Path | None = None) -> "AppPaths":
        base = (root or Path(__file__).resolve().parents[2]).resolve()
        configured = os.environ.get("DART_MCP_DATA_DIR")
        data = Path(configured).expanduser().resolve() if configured else base / "_local_data"
        return cls(base, data, data / "cache", data / "logs", data / "temp", data / "usage")

    def ensure(self, *names: str) -> None:
        for name in names:
            path = getattr(self, name)
            path.mkdir(parents=True, exist_ok=True)
