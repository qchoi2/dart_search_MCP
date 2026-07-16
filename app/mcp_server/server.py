"""Dependency-free MCP JSON-RPC stdio server for the Stage 1 tools."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from app.channels.dart_fulltext import DartFulltextClient
from app.channels.opendart import OpenDartClient
from app.config.env import get_opendart_api_key, load_env_file
from app.config.paths import AppPaths
from app.config.settings import load_settings
from app.contracts import SearchRequest
from app.errors import SearchError
from app.orchestrator.engine import SearchEngine
from app.storage.audit_log import AuditLog

from .tool_contracts import EVIDENCE_TOOL, SEARCH_TOOL


def build_engine() -> SearchEngine:
    paths = AppPaths.discover()
    load_env_file(paths.local_data / ".env")
    settings = load_settings(paths.root / "settings.json")
    key = get_opendart_api_key()
    opendart = OpenDartClient(key) if key else None
    dart = DartFulltextClient() if settings.get("features.dart_fulltext", True) else None
    audit = None
    if settings.get("audit.enabled", True):
        paths.ensure("logs")
        audit = AuditLog(paths.logs / "search_audit.jsonl")
    def resolve_company(name: str) -> str | None:
        if opendart is None:
            return None
        paths.ensure("cache")
        matches = opendart.load_company_directory(paths.cache / "corpCode.zip").lookup(name, limit=2)
        return matches[0].corp_code if len(matches) == 1 else None

    return SearchEngine(opendart=opendart, dart=dart, audit=audit, company_resolver=resolve_company)


class McpApplication:
    def __init__(self, engine: SearchEngine | None = None):
        self.engine = engine or build_engine()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "search_disclosure_cases":
                return self.engine.execute(SearchRequest(**arguments))
            if name == "get_disclosure_evidence":
                return self.engine.get_evidence(**arguments)
            raise ValueError(f"unknown tool: {name}")
        except (SearchError, ValueError, TypeError) as exc:
            if isinstance(exc, SearchError):
                error = exc.to_dict()
            else:
                error = {"code": "INVALID_ARGUMENT", "message": str(exc), "retryable": False}
            return {"status": "failed", "schema_version": "1.0", "error": error}

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "dart-disclosure-search", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": [SEARCH_TOOL, EVIDENCE_TOOL]}
        elif method == "tools/call":
            params = request.get("params") or {}
            payload = self.call_tool(params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload, "isError": payload.get("status") == "failed"}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    app = McpApplication()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = app.handle(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": type(exc).__name__}}, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
