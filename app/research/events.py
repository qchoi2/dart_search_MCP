"""On-demand event chronology graph with explicit-party confidence rules."""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

from app.contracts import DisclosureCandidate


_PARTY_VALUE = r"([가-힣A-Za-z0-9㈜() .&-]{2,60}?)"
_PARTY_BOUNDARY = (
    r"(?=\s+(?:공개매수자(?:의\s*성명|명)?|공개매수\s*대상회사명|"
    r"공개매수할\s*주식등의\s*발행인|주식교환\s*상대회사|완전자회사)\s*[:：]?|[\t\n|]|$)"
)
_PARTY_PATTERNS = {
    "offeror": re.compile(
        r"공개매수자(?:의\s*성명|명)?\s*[:：]?\s*" + _PARTY_VALUE + _PARTY_BOUNDARY
    ),
    "target_company": re.compile(
        r"(?:공개매수\s*대상회사명|공개매수할\s*주식등의\s*발행인[^:：\t\n]{0,20}회사명)"
        r"\s*[:：]?\s*" + _PARTY_VALUE + _PARTY_BOUNDARY
    ),
    "exchange_counterparty": re.compile(
        r"(?:주식교환\s*상대회사|완전자회사(?:가\s*되는\s*회사)?)[^:：\t\n]{0,20}"
        r"\s*[:：]?\s*" + _PARTY_VALUE + _PARTY_BOUNDARY
    ),
}


def _clean_party(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" .,:：")
    return re.sub(r"^(?:주식회사|㈜)|(?:주식회사)$", "", value).strip().casefold()


def classify_event(report_name: str) -> str:
    compact = re.sub(r"\s+", "", report_name)
    if "공개매수결과" in compact:
        return "tender_offer_result"
    if "공개매수신고" in compact:
        return "tender_offer_filing"
    if "공개매수설명" in compact:
        return "tender_offer_explanatory_statement"
    if "공개매수에관한의견" in compact:
        return "target_company_opinion"
    if "주식교환" in compact or "주식이전" in compact:
        return "share_exchange"
    return "other"


def extract_event_parties(text: str) -> dict[str, str]:
    # Preserve row/cell boundaries. They are evidence boundaries and prevent a
    # party value from consuming a following field label.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \f\v]+", " ", normalized)
    parties: dict[str, str] = {}
    for role, pattern in _PARTY_PATTERNS.items():
        match = pattern.search(normalized)
        if match:
            parties[role] = _clean_party(match.group(1))
    return parties


def build_event_graph(candidates: Iterable[DisclosureCandidate], texts: dict[str, str]) -> dict:
    nodes = []
    for candidate in candidates:
        parties = extract_event_parties(texts.get(candidate.receipt_no, ""))
        event_type = classify_event(candidate.report_name)
        material = "|".join((candidate.receipt_no, event_type, *sorted(parties.values())))
        nodes.append({
            "event_id": "event_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20],
            "receipt_no": candidate.receipt_no,
            "receipt_date": candidate.receipt_date,
            "corp_name": candidate.corp_name,
            "event_type": event_type,
            "parties": parties,
            "party_confidence": "confirmed" if parties else "uncertain",
        })
    edges = []
    tender_types = {"tender_offer_filing", "tender_offer_explanatory_statement", "tender_offer_result"}
    for earlier in nodes:
        if earlier["event_type"] not in tender_types:
            continue
        for later in nodes:
            if later["event_type"] != "share_exchange" or later["receipt_date"] < earlier["receipt_date"]:
                continue
            tender_parties = set(earlier["parties"].values())
            exchange_parties = set(later["parties"].values())
            shared = sorted(tender_parties & exchange_parties)
            if shared:
                edges.append({
                    "from_event_id": earlier["event_id"],
                    "to_event_id": later["event_id"],
                    "relation": "tender_offer_precedes_share_exchange",
                    "shared_parties": shared,
                    "confidence": "confirmed",
                    "confirmed": True,
                })
            elif earlier["corp_name"] == later["corp_name"]:
                edges.append({
                    "from_event_id": earlier["event_id"],
                    "to_event_id": later["event_id"],
                    "relation": "possible_temporal_sequence",
                    "shared_parties": [],
                    "confidence": "uncertain",
                    "confirmed": False,
                })
    return {
        "nodes": nodes,
        "edges": edges,
        "confirmed_edge_count": sum(1 for edge in edges if edge["confirmed"]),
        "uncertain_edge_count": sum(1 for edge in edges if not edge["confirmed"]),
    }
