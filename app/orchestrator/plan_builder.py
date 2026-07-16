"""Pure, network-free SearchRequest to immutable SearchPlan planning."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.config import defaults
from app.contracts import SearchPlan, SearchRequest


@lru_cache(maxsize=1)
def search_term_rules() -> dict:
    path = Path(__file__).resolve().parents[1] / "rules" / "search_terms.yaml"
    return json.loads(path.read_text(encoding="utf-8"))


def query_variants(query: str) -> tuple[str, ...]:
    rules = search_term_rules()
    compact = " ".join(query.split())
    variants: list[str] = []
    if "상계" in compact or "납입" in compact:
        variants.extend(rules["precise"])
    if "출자전환" in compact:
        variants.extend(rules["concept"])
    broad_requested = any(marker in compact.casefold() for marker in ("broad", "광범위", "넓게"))
    broad_literal = any(term in compact for term in rules["broad_only"])
    if broad_requested or broad_literal:
        variants.extend(rules["broad_only"])
    if not variants:
        variants.append(compact)
    return tuple(dict.fromkeys(value for value in variants if value))


def choose_strategy(request: SearchRequest) -> tuple[str, str, tuple[str, ...]]:
    query = request.query
    listing = request.company and any(word in query for word in ("목록", "공시내역", "제출내역"))
    if listing:
        return "S1_company_disclosure_list", "opendart", ("dart_fulltext",)
    if request.company:
        return "S2_company_fulltext", "dart_fulltext", ("opendart",)
    return "S3_market_rare_phrase", "dart_fulltext", ("opendart",)


def build_search_plan(request: SearchRequest) -> SearchPlan:
    strategy, primary, secondary = choose_strategy(request)
    if request.mode == "fast":
        list_budget = defaults.FAST_LIST_REQUEST_BUDGET
        dart_budget = defaults.FAST_DART_REQUEST_BUDGET
        document_budget = defaults.FAST_DOCUMENT_BUDGET
        result_budget = min(request.target_count, defaults.FAST_RESULT_BUDGET)
        soft = defaults.FAST_SOFT_TIMEOUT_SECONDS
        hard = defaults.FAST_HARD_TIMEOUT_SECONDS
    else:
        list_budget = defaults.STANDARD_LIST_REQUEST_BUDGET
        dart_budget = defaults.STANDARD_DART_REQUEST_BUDGET
        # Scale down below the 20-result ceiling, while preserving validation headroom.
        document_budget = min(defaults.STANDARD_DOCUMENT_BUDGET, max(request.target_count * 2, request.target_count))
        result_budget = min(request.target_count, defaults.STANDARD_RESULT_BUDGET)
        soft = defaults.STANDARD_SOFT_TIMEOUT_SECONDS
        hard = defaults.STANDARD_HARD_TIMEOUT_SECONDS
    user_ceiling = request.max_documents or document_budget
    effective = min(document_budget, user_ceiling, defaults.AMENDMENT_DOCUMENT_BUDGET_MAX)
    return SearchPlan(
        strategy=strategy,
        primary_channel=primary,
        secondary_channels=secondary,
        query_variants=query_variants(request.query),
        list_request_budget=list_budget,
        dart_request_budget=dart_budget,
        strategy_document_budget=document_budget,
        user_document_ceiling=user_ceiling,
        effective_document_budget=effective,
        estimated_verified_cases=request.target_count,
        estimated_average_chain_length=None,
        chain_length_estimation_basis="not_applicable_fast_path",
        company_count=1 if request.company else 0,
        company_batch_count=1 if request.company else 0,
        result_budget=result_budget,
        preliminary_budget=defaults.STANDARD_PRELIMINARY_BUDGET,
        first_candidate_target_seconds=defaults.FIRST_CANDIDATE_TARGET_SECONDS,
        soft_timeout_seconds=soft,
        hard_timeout_seconds=hard,
        max_escalations=defaults.MAX_ESCALATIONS,
        batch_threshold=(("estimated_documents", defaults.BATCH_ESTIMATED_DOCUMENT_THRESHOLD), ("estimated_seconds", defaults.BATCH_ESTIMATED_SECONDS_THRESHOLD)),
    )
