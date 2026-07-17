"""Conservative field-level comparison for correction disclosures."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation


FIELD_LABELS = (
    "납입일",
    "납입기일",
    "거래대금",
    "양수금액",
    "양도금액",
    "전환가액",
    "발행가액",
    "교환가액",
    "주식교환일",
    "합병기일",
    "주주총회예정일자",
    "신주상장예정일",
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" |;,:：")[:500]


def _date_value(value: str) -> date | None:
    match = re.search(r"(20\d{2})\s*[년./-]\s*(\d{1,2})\s*[월./-]\s*(\d{1,2})\s*일?", value)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _number_value(value: str) -> Decimal | None:
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value.replace(" ", ""))
    if not match:
        return None
    try:
        return Decimal(match.group().replace(",", ""))
    except InvalidOperation:
        return None


def classify_change(before: str, after: str) -> str:
    before_clean, after_clean = _clean(before), _clean(after)
    if before_clean == after_clean:
        return "unchanged"
    if not before_clean and after_clean:
        return "added"
    if before_clean and not after_clean:
        return "removed"
    before_date, after_date = _date_value(before_clean), _date_value(after_clean)
    if before_date and after_date:
        return "postponed" if after_date > before_date else "advanced"
    before_number, after_number = _number_value(before_clean), _number_value(after_clean)
    if before_number is not None and after_number is not None:
        return "increased" if after_number > before_number else "decreased"
    return "text_changed"


def extract_structured_fields(text: str) -> dict[str, str]:
    normalized = re.sub(r"\s+", " ", text)
    result: dict[str, str] = {}
    value_pattern = r"(20\d{2}\s*[년./-]\s*\d{1,2}\s*[월./-]\s*\d{1,2}\s*일?|[-+]?\d[\d,]*(?:\.\d+)?\s*(?:원|주|억원|백만원)?)"
    for label in FIELD_LABELS:
        match = re.search(re.escape(label) + r"\s*[:：]?\s*" + value_pattern, normalized)
        if match:
            result[label] = _clean(match.group(1))
    return result


def compare_structured_fields(before_text: str, after_text: str) -> list[dict[str, str]]:
    before, after = extract_structured_fields(before_text), extract_structured_fields(after_text)
    changes: list[dict[str, str]] = []
    for field in FIELD_LABELS:
        old, new = before.get(field), after.get(field)
        if old is None and new is None:
            continue
        direction = classify_change(old or "", new or "")
        if direction == "unchanged":
            continue
        changes.append({
            "field": field,
            "before": old or "",
            "after": new or "",
            "direction": direction,
            "source": "field_alignment",
            "confidence": "confirmed" if old is not None and new is not None else "uncertain",
        })
    return changes
