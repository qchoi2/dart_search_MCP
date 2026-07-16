"""Mechanical keyword evidence extraction from normalized text."""

from __future__ import annotations

import re

from app.config.defaults import EVIDENCE_MAX_CHARS, EVIDENCE_MAX_SNIPPETS
from app.contracts import EvidenceSnippet


def extract_evidence(
    receipt_no: str,
    text: str,
    keywords: list[str] | tuple[str, ...],
    *,
    max_snippets: int = EVIDENCE_MAX_SNIPPETS,
    max_chars: int = EVIDENCE_MAX_CHARS,
) -> tuple[EvidenceSnippet, ...]:
    matches: list[tuple[int, str]] = []
    folded = text.casefold()
    for keyword in dict.fromkeys(k.strip() for k in keywords if k.strip()):
        start = 0
        needle = keyword.casefold()
        while len(matches) < max_snippets * max(1, len(keywords)):
            index = folded.find(needle, start)
            if index < 0:
                break
            matches.append((index, keyword))
            start = index + max(1, len(needle))
    snippets: list[EvidenceSnippet] = []
    occupied: list[tuple[int, int]] = []
    for index, keyword in sorted(matches)[: max_snippets * 2]:
        left = max(0, index - max_chars // 2)
        right = min(len(text), left + max_chars)
        if any(not (right <= a or left >= b) for a, b in occupied):
            continue
        excerpt = re.sub(r"\s+", " ", text[left:right]).strip()
        snippets.append(EvidenceSnippet(receipt_no, excerpt, (keyword,), start_offset=left, end_offset=right))
        occupied.append((left, right))
        if len(snippets) >= max_snippets:
            break
    return tuple(snippets)
