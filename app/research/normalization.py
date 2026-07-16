"""OpenDART rm/report-name normalization rules."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _rules() -> dict:
    path = Path(__file__).resolve().parents[1] / "rules" / "amendment_rules.yaml"
    return json.loads(path.read_text(encoding="utf-8"))


def parse_rm(raw: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    value = (raw or "").strip()
    known = set(_rules()["rm_meanings"])
    flags: list[str] = []
    unknown: list[str] = []
    for char in value:
        (flags if char in known else unknown).append(char)
    return tuple(flags), tuple(unknown)


def parse_report_name(value: str) -> tuple[tuple[str, ...], str, bool]:
    remaining = value.strip()
    official = tuple(_rules()["official_report_name_prefixes"])
    prefixes: list[str] = []
    while remaining.startswith("["):
        matched = next((prefix for prefix in official if remaining.startswith(prefix)), None)
        if matched is None:
            end = remaining.find("]")
            if end < 0:
                break
            prefixes.append(remaining[: end + 1])
            remaining = remaining[end + 1 :].lstrip()
            continue
        prefixes.append(matched)
        remaining = remaining[len(matched) :].lstrip()
    unknown_combination = any(prefix not in official for prefix in prefixes)
    return tuple(prefixes), " ".join(remaining.split()), unknown_combination


def dart_viewer_url(receipt_no: str) -> str:
    if not receipt_no.isdigit() or len(receipt_no) != 14:
        raise ValueError("receipt_no must contain 14 digits")
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}"
