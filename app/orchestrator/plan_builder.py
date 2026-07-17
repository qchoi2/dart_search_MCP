"""Pure, network-free SearchRequest to immutable SearchPlan planning."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.config import defaults
from app.contracts import SearchPlan, SearchRequest
from app.rules.validation import load_rule_file


@lru_cache(maxsize=1)
def search_term_rules() -> dict:
    path = Path(__file__).resolve().parents[1] / "rules" / "search_terms.yaml"
    return load_rule_file(path, "search_terms")


def query_variants(query: str) -> tuple[str, ...]:
    rules = search_term_rules()
    compact = " ".join(query.split())
    variants: list[str] = []
    has_setoff = "상계" in compact or "납입" in compact
    has_conversion = "출자전환" in compact
    if has_setoff:
        variants.extend(rules["precise"])
    if has_conversion:
        variants.extend(rules["concept"])
    broad_requested = any(marker in compact.casefold() for marker in ("broad", "광범위", "넓게"))
    broad_literal = any(term in compact for term in rules["broad_only"])
    if broad_requested or broad_literal:
        if has_setoff:
            variants.extend(rules["broad_only"][:2])
        if has_conversion:
            variants.extend(rules["broad_only"][2:])
    if not variants:
        variants.append(compact)
    return tuple(dict.fromkeys(value for value in variants if value))


def choose_strategy(request: SearchRequest) -> tuple[str, str, tuple[str, ...]]:
    query = request.query
    if request.amendment_comparison:
        return "S6_amendment_comparison", "dart_fulltext", ("opendart",)
    if any(marker in query for marker in ("최종 유효본", "최종유효본", "철회 여부", "철회여부")):
        return "S7_effective_filing", "dart_fulltext", ("opendart",)
    if request.sequence_required:
        return "S5_event_sequence", "dart_fulltext", ("opendart",)
    listing = request.company and any(word in query for word in ("목록", "공시내역", "제출내역"))
    if listing:
        return "S1_company_disclosure_list", "opendart", ("dart_fulltext",)
    if request.company:
        return "S2_company_fulltext", "dart_fulltext", ("opendart",)
    return "S3_market_rare_phrase", "dart_fulltext", ("opendart",)


def strategy_query_variants(request: SearchRequest, strategy: str) -> tuple[str, ...]:
    if strategy == "S5_event_sequence":
        variants = [term for term in ("공개매수", "주식교환", "주식이전") if term in request.query]
        return tuple(variants or (request.query.strip(),))
    if strategy == "S6_amendment_comparison":
        from app.research.structural_diff import FIELD_LABELS

        fields = [field for field in FIELD_LABELS if field in request.query]
        if fields:
            return tuple(fields)
        stripped = request.query
        for marker in ("정정 전후", "정정전후", "정정 비교", "정정비교", "정정된 사례", "정정 사례"):
            stripped = stripped.replace(marker, " ")
        stripped = " ".join(stripped.split())
        return (stripped or "정정사항",)
    if strategy == "S7_effective_filing":
        stripped = request.query
        for marker in ("최종 유효본", "최종유효본", "철회 여부", "철회여부"):
            stripped = stripped.replace(marker, " ")
        stripped = " ".join(stripped.split())
        return (stripped or "정정사항",)
    return query_variants(request.query)


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
        if strategy in {"S6_amendment_comparison", "S7_effective_filing"}:
            document_budget = defaults.AMENDMENT_DOCUMENT_BUDGET
        else:
            document_budget = min(
                defaults.STANDARD_DOCUMENT_BUDGET,
                max(request.target_count * defaults.DOCUMENT_CANDIDATE_HEADROOM_MULTIPLIER, request.target_count),
            )
        result_budget = min(request.target_count, defaults.STANDARD_RESULT_BUDGET)
        soft = defaults.STANDARD_SOFT_TIMEOUT_SECONDS
        hard = defaults.STANDARD_HARD_TIMEOUT_SECONDS
    user_ceiling = request.max_documents or document_budget
    effective = min(document_budget, user_ceiling, defaults.DOCUMENT_BUDGET_ABSOLUTE_MAX)
    return SearchPlan(
        strategy=strategy,
        primary_channel=primary,
        secondary_channels=secondary,
        query_variants=strategy_query_variants(request, strategy),
        list_request_budget=list_budget,
        dart_request_budget=dart_budget,
        strategy_document_budget=document_budget,
        user_document_ceiling=user_ceiling,
        effective_document_budget=effective,
        estimated_verified_cases=request.target_count,
        estimated_average_chain_length=None,
        chain_length_estimation_basis=(
            "explicit_relation_sampling_pending"
            if strategy in {"S6_amendment_comparison", "S7_effective_filing"}
            else "not_applicable_fast_path"
        ),
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
