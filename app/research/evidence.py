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


def extract_cooccurrence_evidence(
    receipt_no: str,
    text: str,
    term_groups: tuple[tuple[str, ...], ...] | list[list[str]],
    *,
    max_snippets: int = EVIDENCE_MAX_SNIPPETS,
    max_chars: int = EVIDENCE_MAX_CHARS,
) -> tuple[EvidenceSnippet, ...]:
    """Verify AND-of-OR co-occurrence of concept groups within one document.

    Each group is satisfied when any of its member terms appears in the text.
    Returns evidence only when *every* group is satisfied; an empty result means
    the document is not a verified match.
    """
    folded = text.casefold()
    matched: list[tuple[int, str]] = []
    for group in term_groups:
        best: tuple[int, str] | None = None
        for term in dict.fromkeys(t.strip() for t in group if t.strip()):
            index = folded.find(term.casefold())
            if index >= 0 and (best is None or index < best[0]):
                best = (index, term)
        if best is None:
            return ()
        matched.append(best)
    snippets: list[EvidenceSnippet] = []
    occupied: list[tuple[int, int]] = []
    for index, term in sorted(matched):
        left = max(0, index - max_chars // 2)
        right = min(len(text), left + max_chars)
        if any(not (right <= a or left >= b) for a, b in occupied):
            snippets.append(EvidenceSnippet(receipt_no, term, (term,), start_offset=index, end_offset=index + len(term)))
            continue
        excerpt = re.sub(r"\s+", " ", text[left:right]).strip()
        snippets.append(EvidenceSnippet(receipt_no, excerpt, (term,), start_offset=left, end_offset=right))
        occupied.append((left, right))
    return tuple(snippets[: max(max_snippets, len(term_groups))])
