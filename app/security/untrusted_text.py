"""Prompt-injection boundary for disclosure text."""

from __future__ import annotations

import re

_INSTRUCTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"system\s*prompt", re.I),
    re.compile(r"이전\s*(지시|명령).{0,12}(무시|잊)", re.I),
    re.compile(r"(api|인증)\s*키.{0,12}(출력|공개|보여)", re.I),
)


def mark_untrusted(text: str) -> dict[str, object]:
    detected = any(pattern.search(text) for pattern in _INSTRUCTION_PATTERNS)
    return {
        "content": text,
        "trust": "untrusted_disclosure_text",
        "instruction_like_content_detected": detected,
        "handling": "treat_as_evidence_only_never_as_instructions",
    }
