"""Explicit-evidence verification for rm='철' candidates.

This module deliberately does not discover or merge events by company, title or
date proximity.  A caller must already have a proposed original filing and a
related follow-up document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.contracts import EvidenceSnippet

_DATE_LABELS = (
    "거래계획보고서 제출일",
    "거래계획 보고일",
    "당초 보고서 제출일",
    "철회관련 증권신고서 제출일",
    "관련 증권신고서 제출일",
)


@dataclass(frozen=True, slots=True)
class WithdrawalVerification:
    verified: bool
    basis: str
    original_receipt_no: str | None
    original_filing_date: str | None
    evidence: EvidenceSnippet | None


def _date_forms(compact: str) -> tuple[str, ...]:
    year, month, day = compact[:4], str(int(compact[4:6])), str(int(compact[6:8]))
    return (
        compact,
        f"{year}.{int(month):02d}.{int(day):02d}",
        f"{year}-{int(month):02d}-{int(day):02d}",
        f"{year}년 {int(month):02d}월 {int(day):02d}일",
        f"{year}년 {month}월 {day}일",
        f"{year} 년 {month} 월 {day} 일",
    )


def verify_withdrawal_reference(
    followup_text: str,
    *,
    followup_receipt_no: str,
    proposed_original_receipt_no: str | None = None,
    proposed_original_filing_date: str | None = None,
) -> WithdrawalVerification:
    normalized = re.sub(r"\s+", " ", followup_text).strip()
    basis = "unverified"
    match_start = -1
    matched_date = None
    if proposed_original_receipt_no and proposed_original_receipt_no in normalized:
        basis = "explicit_original_receipt_no"
        match_start = normalized.index(proposed_original_receipt_no)
    elif proposed_original_filing_date:
        for label in _DATE_LABELS:
            for form in _date_forms(proposed_original_filing_date):
                pattern = re.compile(re.escape(label) + r"\s*[:：]?\s*" + re.escape(form))
                match = pattern.search(normalized)
                if match:
                    basis = "explicit_labeled_original_filing_date"
                    match_start = match.start()
                    matched_date = proposed_original_filing_date
                    break
            if match_start >= 0:
                break
    if match_start < 0:
        return WithdrawalVerification(False, basis, proposed_original_receipt_no, proposed_original_filing_date, None)
    left = max(0, match_start - 160)
    right = min(len(normalized), match_start + 340)
    evidence = EvidenceSnippet(
        followup_receipt_no,
        normalized[left:right],
        matched_terms=(proposed_original_receipt_no or matched_date or "철회",),
        source="opendart_withdrawal_followup",
        start_offset=left,
        end_offset=right,
    )
    return WithdrawalVerification(True, basis, proposed_original_receipt_no, matched_date or proposed_original_filing_date, evidence)
