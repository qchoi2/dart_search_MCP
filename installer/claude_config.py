"""Safe fallback registration for Claude Desktop development installs.

The release package uses MCPB. These helpers exist for diagnostics and repair
of older manual installations, including Microsoft Store/MSIX Claude builds.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SERVER_NAME = "dart-disclosure-search"


@dataclass(frozen=True)
class ConfigCandidate:
    path: Path
    kind: str


def config_candidates(*, appdata: Path, localappdata: Path) -> list[ConfigCandidate]:
    values = [ConfigCandidate(appdata / "Claude" / "claude_desktop_config.json", "standard")]
    packages = localappdata / "Packages"
    if packages.exists():
        for package in sorted(packages.glob("Claude_*")):
            values.append(
                ConfigCandidate(
                    package / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json",
                    "msix",
                )
            )
    return values


def discover_config(*, appdata: Path | None = None, localappdata: Path | None = None) -> ConfigCandidate:
    roaming = appdata or Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    local = localappdata or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    candidates = config_candidates(appdata=roaming, localappdata=local)
    existing = [item for item in candidates if item.path.exists()]
    msix_existing = [item for item in existing if item.kind == "msix"]
    if msix_existing:
        return max(msix_existing, key=lambda item: item.path.stat().st_mtime)
    if existing:
        return max(existing, key=lambda item: item.path.stat().st_mtime)
    return candidates[0]


def _read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Claude Desktop 설정 파일의 최상위 값은 객체여야 합니다.")
    servers = parsed.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Claude Desktop 설정의 mcpServers 값이 객체가 아닙니다.")
    return parsed


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.backup-{stamp}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.backup-{stamp}-{counter}")
        counter += 1
    shutil.copy2(path, target)
    return target


def register_server(path: Path, server_config: dict) -> dict:
    current = _read_config(path)
    updated = json.loads(json.dumps(current))
    servers = updated.setdefault("mcpServers", {})
    if servers.get(SERVER_NAME) == server_config:
        return {"status": "unchanged", "path": str(path), "backup": None}
    backup = _backup(path)
    servers[SERVER_NAME] = server_config
    _atomic_json(path, updated)
    return {"status": "updated", "path": str(path), "backup": str(backup) if backup else None}


def unregister_server(path: Path) -> dict:
    current = _read_config(path)
    servers = current.setdefault("mcpServers", {})
    if SERVER_NAME not in servers:
        return {"status": "unchanged", "path": str(path), "backup": None}
    backup = _backup(path)
    del servers[SERVER_NAME]
    _atomic_json(path, current)
    return {"status": "removed", "path": str(path), "backup": str(backup) if backup else None}


def find_registrations(candidates: Iterable[ConfigCandidate]) -> list[dict]:
    registrations = []
    for item in candidates:
        try:
            config = _read_config(item.path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            registrations.append({"path": str(item.path), "kind": item.kind, "status": "invalid", "detail": str(exc)})
            continue
        status = "registered" if SERVER_NAME in config.get("mcpServers", {}) else "not_registered"
        registrations.append({"path": str(item.path), "kind": item.kind, "status": status})
    return registrations
