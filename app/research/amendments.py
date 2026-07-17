"""On-demand S6/S7 amendment relationship and correction-table analysis."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable

from app.contracts import DisclosureCandidate
from app.research.structural_diff import classify_change, compare_structured_fields


_RECEIPT_LABEL = re.compile(
    r"(?:원\s*공시|원\s*접수|최초\s*접수|정정\s*대상|당초\s*보고서|관련\s*공시)[^0-9]{0,50}(20\d{12})"
)
_DATE_LABEL = re.compile(
    r"(?:공시서류의\s*최초제출일|정정관련\s*공시서류제출일|당초\s*보고서\s*제출일)\s*[:：]?\s*"
    r"(20\d{2})\s*[년./-]\s*(\d{1,2})\s*[월./-]\s*(\d{1,2})\s*일?"
)
_ROW_PATTERN = re.compile(
    r"(?P<field>[가-힣A-Za-z0-9()ㆍ·./%\s]{1,40}?)\s+"
    r"(?:정정사유\s*[:：]?\s*(?P<reason>[^;|\n]{1,80}?)\s+)?"
    r"정\s*정\s*전\s*[:：]?\s*(?P<before>.*?)\s+"
    r"정\s*정\s*후\s*[:：]?\s*(?P<after>.*?)(?=\n|;|\||$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CorrectionRow:
    field: str
    reason: str | None
    before: str
    after: str
    direction: str
    confidence: str = "confirmed"


@dataclass(frozen=True, slots=True)
class AmendmentContext:
    receipt_no: str
    original_receipt_no: str | None
    original_filing_date: str | None
    relation_basis: str | None
    correction_rows: tuple[CorrectionRow, ...]
    has_correction_table: bool


def _compact(value: str, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", value).strip(" |;,:：")[:limit]


def extract_amendment_context(text: str, *, receipt_no: str) -> AmendmentContext:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    receipt_match = _RECEIPT_LABEL.search(normalized)
    original_receipt = receipt_match.group(1) if receipt_match and receipt_match.group(1) != receipt_no else None
    date_match = _DATE_LABEL.search(normalized)
    original_date = None
    if date_match:
        original_date = f"{int(date_match.group(1)):04d}{int(date_match.group(2)):02d}{int(date_match.group(3)):02d}"
    rows: list[CorrectionRow] = []
    lines = normalized.split("\n")
    for line_index, line in enumerate(lines):
        cells = [_compact(cell, 500) for cell in line.split("\t")]
        compact_headers = [re.sub(r"\s+", "", cell) for cell in cells]
        before_index = next((index for index, cell in enumerate(compact_headers) if cell in {"정정전", "정정前"}), None)
        after_index = next((index for index, cell in enumerate(compact_headers) if cell in {"정정후", "정정後"}), None)
        if before_index is None or after_index is None:
            continue
        field_index = next((index for index, cell in enumerate(compact_headers) if cell in {"항목", "정정항목"}), 0)
        reason_index = next((index for index, cell in enumerate(compact_headers) if cell == "정정사유"), None)
        for row_line in lines[line_index + 1 : line_index + 31]:
            row_cells = [_compact(cell, 500) for cell in row_line.split("\t")]
            if max(field_index, before_index, after_index) >= len(row_cells):
                break
            field, before, after = row_cells[field_index], row_cells[before_index], row_cells[after_index]
            if not field or (not before and not after):
                continue
            reason = row_cells[reason_index] if reason_index is not None and reason_index < len(row_cells) else None
            rows.append(CorrectionRow(field, reason or None, before, after, classify_change(before, after)))
        if rows:
            break
    if not rows:
        for line in lines:
            for match in _ROW_PATTERN.finditer(line):
                field = _compact(match.group("field"), 80)
                raw_before, raw_after = match.group("before"), match.group("after")
                before, after = _compact(raw_before), _compact(raw_after)
                invalid_field = (
                    any(marker in field for marker in ("정정사항", "정정사유", "보고서"))
                    or bool(re.search(r"20\d{2}년", field))
                )
                if invalid_field or not field or not before or not after or len(raw_before) > 300 or len(raw_after) > 300:
                    continue
                rows.append(CorrectionRow(
                    field=field,
                    reason=_compact(match.group("reason"), 120) if match.group("reason") else None,
                    before=before,
                    after=after,
                    direction=classify_change(before, after),
                ))
    basis = "explicit_original_receipt_no" if original_receipt else "explicit_labeled_original_filing_date" if original_date else None
    has_table = bool(rows) or bool(re.search(r"정\s*정\s*전\s+정\s*정\s*후|정정전\s+정정후", normalized))
    return AmendmentContext(receipt_no, original_receipt, original_date, basis, tuple(rows), has_table)


def _chain_id(receipts: Iterable[str]) -> str:
    material = "|".join(sorted(receipts))
    return "amend_" + hashlib.sha256(material.encode("ascii")).hexdigest()[:20]


def build_amendment_chains(
    candidates: Iterable[DisclosureCandidate],
    contexts: dict[str, AmendmentContext],
) -> list[dict]:
    items = {candidate.receipt_no: candidate for candidate in candidates}
    parent = {receipt: receipt for receipt in items}
    edge_basis: dict[str, str] = {}
    uncertain: set[str] = set()

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for receipt, context in contexts.items():
        if receipt not in items:
            continue
        target = context.original_receipt_no
        if target and target in items:
            union(receipt, target)
            edge_basis[receipt] = "explicit_original_receipt_no"
            continue
        if context.original_filing_date:
            current = items[receipt]
            matches = [
                candidate.receipt_no
                for candidate in items.values()
                if candidate.receipt_date == context.original_filing_date
                and candidate.corp_code == current.corp_code
                and candidate.report_name == current.report_name
                and candidate.receipt_no != receipt
            ]
            if len(matches) == 1:
                union(receipt, matches[0])
                edge_basis[receipt] = "explicit_labeled_original_filing_date_unique_match"
            else:
                uncertain.add(receipt)
        elif context.has_correction_table or items[receipt].report_name_prefixes or "정" in items[receipt].rm_flags:
            uncertain.add(receipt)

    groups: dict[str, list[DisclosureCandidate]] = {}
    for receipt, candidate in items.items():
        groups.setdefault(find(receipt), []).append(candidate)
    chains: list[dict] = []
    for members in groups.values():
        linked = len(members) > 1
        member_receipts = {member.receipt_no for member in members}
        has_amendment_signal = linked or any(
            member.receipt_no in uncertain or member.report_name_prefixes or "정" in member.rm_flags
            for member in members
        )
        if not has_amendment_signal:
            continue
        ordered = sorted(members, key=lambda item: (item.receipt_date, item.receipt_no))
        original = ordered[0]
        final = ordered[-1]
        all_linked = linked and all(
            member is original or member.receipt_no in edge_basis
            for member in ordered
        )
        confidence = "confirmed" if all_linked else "uncertain"
        chain_complete = all_linked
        withdrawal_status = "confirmed" if chain_complete and "철" in final.rm_flags else "not_indicated"
        effective = (
            final.receipt_no
            if chain_complete and "철" not in final.rm_flags and "정" not in final.rm_flags
            else None
        )
        chains.append({
            "amendment_chain_id": _chain_id(member_receipts),
            "member_receipt_nos": [member.receipt_no for member in ordered],
            "original_receipt_no": original.receipt_no if chain_complete else None,
            "final_receipt_no": final.receipt_no if linked else None,
            "effective_receipt_no": effective,
            "effective_status": "provisional" if effective else "unresolved",
            "withdrawal_status": withdrawal_status,
            "chain_complete": chain_complete,
            "chain_confidence": confidence,
            "relation_basis_by_receipt": {key: edge_basis[key] for key in member_receipts if key in edge_basis},
            "warnings": [] if confidence == "confirmed" else ["부분 체인 또는 명시 관계가 부족해 확정하지 않았습니다."],
        })
    return sorted(chains, key=lambda chain: chain["member_receipt_nos"][-1], reverse=True)


def compare_amendment_chain(chain: dict, texts: dict[str, str], contexts: dict[str, AmendmentContext]) -> dict:
    original_no, final_no = chain.get("original_receipt_no"), chain.get("final_receipt_no")
    if not original_no or not final_no or original_no not in texts or final_no not in texts:
        return {
            "amendment_chain_id": chain["amendment_chain_id"],
            "comparison_complete": False,
            "changes": [],
            "warning": "원공시 또는 최종본 원문이 없어 부분 체인 비교만 가능합니다.",
        }
    final_context = contexts.get(final_no)
    if final_context and final_context.correction_rows:
        changes = [
            {
                "field": row.field,
                "reason": row.reason,
                "before": row.before,
                "after": row.after,
                "direction": row.direction,
                "source": "correction_table",
                "confidence": row.confidence,
            }
            for row in final_context.correction_rows
        ]
    else:
        changes = compare_structured_fields(texts[original_no], texts[final_no])
    return {
        "amendment_chain_id": chain["amendment_chain_id"],
        "comparison_complete": True,
        "comparison_basis": "original_to_final",
        "original_receipt_no": original_no,
        "final_receipt_no": final_no,
        "changes": changes,
        "warning": None if changes else "구조화 필드에서 변경을 추출하지 못했습니다.",
    }
