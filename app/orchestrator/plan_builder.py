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


def decompose_query(query: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Split a free-text query into (search_variants, verification_term_groups).

    Search variants are OR-broadened terms drawn from the most selective
    (searchable) synonym groups plus any query-specific leftover tokens; they
    are what the DART full-text channel searches. Verification term groups are
    AND-of-OR: a case is only verified when at least one member of every group
    co-occurs in the document. This keeps search broad while keeping
    verification strict, so a whole-sentence query no longer becomes a single
    verbatim substring that can never match.
    """
    rules = search_term_rules()
    groups_cfg = rules.get("synonym_groups", {})
    filler = set(rules.get("filler", []))
    report_names = rules.get("report_name_terms", [])
    compact = " ".join(query.split())
    # Strip report-name (title) tokens: a body document does not reliably repeat
    # its own report title, and true title-constrained search is a probe-gated
    # follow-up (see DECISIONS.md, reportName+keyword measurement).
    stripped = compact
    for name in report_names:
        stripped = stripped.replace(name, " ")
    tokens = [token for token in stripped.split() if len(token) >= 2 and token not in filler]

    verification_groups: list[tuple[str, ...]] = []
    group_search: list[str] = []
    covered: set[str] = set()
    for record in groups_cfg.values():
        terms = tuple(record.get("terms", ()))
        if not terms or not any(term in compact for term in terms):
            continue
        verification_groups.append(terms)
        covered.update(token for token in tokens if any(term in token or token in term for term in terms))
        if record.get("searchable"):
            group_search.extend(terms)

    leftover_search: list[str] = []
    for token in tokens:
        if token in covered:
            continue
        verification_groups.append((token,))
        leftover_search.append(token)
        covered.add(token)

    search_variants = list(dict.fromkeys(value for value in (*leftover_search, *group_search) if value))
    if not search_variants or not verification_groups:
        return (compact,), ()
    return tuple(search_variants[: defaults.DECOMPOSED_SEARCH_VARIANT_MAX]), tuple(verification_groups)


def resolve_query_terms(query: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Body-search strategies: hardcoded set-off/conversion rules keep their
    legacy OR verification (empty groups); everything else is decomposed into
    broadened variants plus AND-of-OR co-occurrence groups."""
    compact = " ".join(query.split())
    if "상계" in compact or "납입" in compact or "출자전환" in compact:
        return query_variants(query), ()
    return decompose_query(query)


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


def strategy_terms(request: SearchRequest, strategy: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Return (query_variants, verification_term_groups) for a strategy.

    Only the body full-text strategies (S2/S3) use co-occurrence groups; the
    relation strategies and the company listing verify by other means and keep
    empty groups (legacy verification).
    """
    if strategy == "S5_event_sequence":
        variants = [term for term in ("공개매수", "주식교환", "주식이전") if term in request.query]
        return tuple(variants or (request.query.strip(),)), ()
    if strategy == "S6_amendment_comparison":
        from app.research.structural_diff import FIELD_LABELS

        fields = [field for field in FIELD_LABELS if field in request.query]
        if fields:
            return tuple(fields), ()
        stripped = request.query
        for marker in ("정정 전후", "정정전후", "정정 비교", "정정비교", "정정된 사례", "정정 사례"):
            stripped = stripped.replace(marker, " ")
        stripped = " ".join(stripped.split())
        return (stripped or "정정사항",), ()
    if strategy == "S7_effective_filing":
        stripped = request.query
        for marker in ("최종 유효본", "최종유효본", "철회 여부", "철회여부"):
            stripped = stripped.replace(marker, " ")
        stripped = " ".join(stripped.split())
        return (stripped or "정정사항",), ()
    if strategy == "S1_company_disclosure_list":
        return query_variants(request.query), ()
    return resolve_query_terms(request.query)


def strategy_query_variants(request: SearchRequest, strategy: str) -> tuple[str, ...]:
    return strategy_terms(request, strategy)[0]


def build_search_plan(request: SearchRequest) -> SearchPlan:
    strategy, primary, secondary = choose_strategy(request)
    variants, verification_groups = strategy_terms(request, strategy)
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
        query_variants=variants,
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
        verification_term_groups=verification_groups,
    )
