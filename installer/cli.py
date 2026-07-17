"""Developer diagnostics for manual Claude Desktop registrations."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.config.defaults import PRODUCT_VERSION
from app.mcp_server.server import McpApplication

from .claude_config import (
    config_candidates,
    discover_config,
    find_registrations,
    register_server,
    unregister_server,
)


ROOT = Path(__file__).resolve().parents[1]


def _locations() -> tuple[Path, Path]:
    return (
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")),
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")),
    )


def diagnose() -> dict:
    appdata, localappdata = _locations()
    initialization = McpApplication().handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    return {
        "status": "healthy",
        "version": PRODUCT_VERSION,
        "python": sys.executable,
        "api_key_configured": bool(os.environ.get("DART_API_KEY")),
        "api_key_value": "stored_but_not_displayed" if os.environ.get("DART_API_KEY") else None,
        "initialize_ok": bool(initialization and initialization.get("result")),
        "claude_configs": find_registrations(config_candidates(appdata=appdata, localappdata=localappdata)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="공시검색 MCP 수동 설치 진단·복구 도구")
    parser.add_argument("action", choices=("diagnose", "register", "unregister"), default="diagnose", nargs="?")
    args = parser.parse_args()
    if args.action == "diagnose":
        result = diagnose()
    else:
        appdata, localappdata = _locations()
        candidate = discover_config(appdata=appdata, localappdata=localappdata)
        if args.action == "register":
            result = register_server(
                candidate.path,
                {
                    "command": sys.executable,
                    "args": ["-m", "app.mcp_server.server"],
                    "cwd": str(ROOT),
                    "env": {"PYTHONUTF8": "1"},
                },
            )
        else:
            result = unregister_server(candidate.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
