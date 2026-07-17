"""Immediate schema validation for JSON-compatible runtime rule files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config.defaults import SCHEMA_VERSION

_EVIDENCE_FIELDS = {"evidence_fixture", "sample_count", "sample_scope", "confidence", "checked_at"}


def load_rule_file(path: Path, kind: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path.name}: schema_version must be {SCHEMA_VERSION}")
    if kind == "search_terms":
        for key in ("precise", "concept", "broad_only"):
            if not isinstance(payload.get(key), list) or not all(isinstance(item, str) and item for item in payload[key]):
                raise ValueError(f"{path.name}: {key} must be a non-empty string list")
        for key in ("filler", "report_name_terms"):
            value = payload.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise ValueError(f"{path.name}: {key} must be a string list")
        groups = payload.get("synonym_groups", {})
        if not isinstance(groups, dict):
            raise ValueError(f"{path.name}: synonym_groups must be an object")
        for name, record in groups.items():
            terms = record.get("terms") if isinstance(record, dict) else None
            if not isinstance(record, dict) or not isinstance(record.get("searchable"), bool):
                raise ValueError(f"{path.name}: synonym_groups.{name} needs a boolean 'searchable'")
            if not isinstance(terms, list) or not terms or not all(isinstance(item, str) and item for item in terms):
                raise ValueError(f"{path.name}: synonym_groups.{name}.terms must be a non-empty string list")
    elif kind == "ranking":
        weights = payload.get("weights")
        if not isinstance(weights, dict) or not weights or not all(isinstance(value, (int, float)) for value in weights.values()):
            raise ValueError(f"{path.name}: weights must contain numeric values")
    elif kind == "amendment":
        if not isinstance(payload.get("official_report_name_prefixes"), list):
            raise ValueError(f"{path.name}: official_report_name_prefixes must be a list")
        if not isinstance(payload.get("rm_meanings"), dict):
            raise ValueError(f"{path.name}: rm_meanings must be an object")
        evidence = payload.get("rule_evidence")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError(f"{path.name}: rule_evidence must be a non-empty object")
        for name, record in evidence.items():
            if not isinstance(record, dict) or not _EVIDENCE_FIELDS <= record.keys():
                raise ValueError(f"{path.name}: rule_evidence.{name} is missing required fields")
            count = record["sample_count"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"{path.name}: rule_evidence.{name}.sample_count must be a non-negative integer")
            if count < 3 and record["confidence"] != "provisional":
                raise ValueError(f"{path.name}: sample_count < 3 requires provisional confidence")
    else:
        raise ValueError(f"unknown rule kind: {kind}")
    return payload
