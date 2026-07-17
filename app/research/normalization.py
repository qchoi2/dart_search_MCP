"""OpenDART rm/report-name normalization rules."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.config.defaults import RECEIPT_NO_LENGTH
from app.rules.validation import load_rule_file


@lru_cache(maxsize=1)
def _rules() -> dict:
    path = Path(__file__).resolve().parents[1] / "rules" / "amendment_rules.yaml"
    return load_rule_file(path, "amendment")


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
    if not receipt_no.isdigit() or len(receipt_no) != RECEIPT_NO_LENGTH:
        raise ValueError(f"receipt_no must contain {RECEIPT_NO_LENGTH} digits")
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}"
