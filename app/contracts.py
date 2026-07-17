"""Frozen public data contracts for Stage 1."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any

from app.config import defaults
from app.config.defaults import SCHEMA_VERSION


class ChannelStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    PROBING = "PROBING"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    company: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    target_count: int = defaults.DEFAULT_TARGET_COUNT
    mode: str = "standard"
    max_documents: int | None = None
    cache_mode: str = "auto"
    exhaustive: bool | None = None
    amendment_comparison: bool | None = None
    sequence_required: bool | None = None
    output_mode: str = "interactive"
    continuation_token: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise ValueError("query must be a string")
        if not defaults.MIN_TARGET_COUNT <= len(self.query.strip()) <= defaults.QUERY_MAX_CHARS:
            raise ValueError(f"query must contain {defaults.MIN_TARGET_COUNT} to {defaults.QUERY_MAX_CHARS} characters")
        if self.company is not None and not isinstance(self.company, str):
            raise ValueError("company must be a string or null")
        if isinstance(self.target_count, bool) or not isinstance(self.target_count, int):
            raise ValueError("target_count must be an integer")
        if self.output_mode not in {"interactive", "batch"}:
            raise ValueError("output_mode must be interactive or batch")
        limit = defaults.BATCH_TARGET_MAX if self.output_mode == "batch" else defaults.INTERACTIVE_TARGET_MAX
        if not defaults.MIN_TARGET_COUNT <= self.target_count <= limit:
            raise ValueError(f"target_count must be between {defaults.MIN_TARGET_COUNT} and {limit}")
        if self.max_documents is not None and (isinstance(self.max_documents, bool) or not isinstance(self.max_documents, int)):
            raise ValueError("max_documents must be an integer or null")
        if self.max_documents is not None and not defaults.MIN_TARGET_COUNT <= self.max_documents <= defaults.DOCUMENT_BUDGET_ABSOLUTE_MAX:
            raise ValueError(f"max_documents must be between {defaults.MIN_TARGET_COUNT} and {defaults.DOCUMENT_BUDGET_ABSOLUTE_MAX}")
        if self.cache_mode not in {"auto", "session"}:
            raise ValueError("cache_mode must be auto or session until TTL cache is enabled")
        for name in ("exhaustive", "amendment_comparison", "sequence_required"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean or null")
        if self.continuation_token is not None and not isinstance(self.continuation_token, str):
            raise ValueError("continuation_token must be a string or null")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        for name in ("date_from", "date_to"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be an ISO date string or null")
            if value is not None:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError(f"{name} must use YYYY-MM-DD format") from exc
        if self.date_from and self.date_to:
            if date.fromisoformat(self.date_from) > date.fromisoformat(self.date_to):
                raise ValueError("date_from must be on or before date_to")
        if self.mode not in {"fast", "standard"}:
            raise ValueError("mode must be fast or standard")


@dataclass(frozen=True, slots=True)
class SearchPlan:
    strategy: str
    primary_channel: str
    secondary_channels: tuple[str, ...]
    query_variants: tuple[str, ...]
    list_request_budget: int
    dart_request_budget: int
    strategy_document_budget: int
    user_document_ceiling: int
    effective_document_budget: int
    estimated_verified_cases: int
    estimated_average_chain_length: float | None
    chain_length_estimation_basis: str
    company_count: int
    company_batch_count: int
    result_budget: int
    preliminary_budget: int
    first_candidate_target_seconds: int
    soft_timeout_seconds: int
    hard_timeout_seconds: int
    max_escalations: int
    batch_threshold: tuple[tuple[str, int], ...]
    schema_version: str = SCHEMA_VERSION
    # AND-of-OR concept groups for co-occurrence verification. Empty tuple keeps
    # the legacy behaviour of verifying by any query_variant substring match.
    verification_term_groups: tuple[tuple[str, ...], ...] = ()


@dataclass(slots=True)
class SearchExecutionDiagnostics:
    measured_total_count_by_window: dict[str, int] = field(default_factory=dict)
    measured_total_pages_by_window: dict[str, int] = field(default_factory=dict)
    processed_window_count: int = 0
    sampled_window_count: int = 0
    estimated_remaining_documents: int = 0
    estimation_basis: str = "heuristic"
    estimation_confidence: str = "low"
    actual_list_requests: int = 0
    actual_document_requests: int = 0
    dart_result_page_requests: int = 0
    structure_retry_requests: int = 0
    health_check_requests: int = 0
    mode_setup_requests: int = 0
    cache_hits: int = 0
    first_candidate_elapsed_ms: int | None = None
    completed_elapsed_ms: int | None = None
    channel_health_events: list[dict[str, Any]] = field(default_factory=list)
    latest_first_bias: bool = False
    fallback_used: bool = False
    fully_pageable_by_query: dict[str, bool] = field(default_factory=dict)
    dart_linked_last_page_by_query: dict[str, int | None] = field(default_factory=dict)
    pagination_contract_changed: bool = False
    pagination_contract_observations: list[dict[str, Any]] = field(default_factory=list)
    soft_timeout_reached: bool = False
    hard_timeout_reached: bool = False
    deadline_limited_timeout: bool = False
    deadline_request_start_blocked: bool = False
    deadline_backoff_blocked: bool = False
    processed_receipt_count: int = 0
    unprocessed_candidate_count: int = 0
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class EvidenceSnippet:
    receipt_no: str
    text: str
    matched_terms: tuple[str, ...] = ()
    section: str | None = None
    source: str = "opendart_document"
    start_offset: int | None = None
    end_offset: int | None = None
    untrusted_source: bool = True
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DisclosureCandidate:
    candidate_id: str
    corp_code: str | None
    corp_name: str
    stock_code: str | None
    report_name: str
    report_name_prefixes: tuple[str, ...]
    receipt_no: str
    receipt_date: str
    filer_name: str
    rm_raw: str
    rm_flags: tuple[str, ...]
    unknown_rm_flags: tuple[str, ...]
    market_jurisdiction: str | None
    includes_consolidated_part: bool
    amendment_origin: str | None
    source_channels: tuple[str, ...]
    matched_terms: tuple[str, ...]
    matched_sections: tuple[str, ...]
    fulltext_match_scope: str
    fulltext_row_tags: tuple[str, ...]
    mechanical_score: float
    original_receipt_no: str | None
    amendment_chain_id: str | None
    chain_complete: bool
    chain_confidence: str
    event_id: str | None
    verification_status: str
    evidence: tuple[EvidenceSnippet, ...] = ()
    dart_viewer_url: str | None = None
    unknown_prefix_combination: bool = False
    rm_combination_confidence: str = "not_applicable"
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class VerifiedCase:
    case_id: str
    case_title: str
    companies: tuple[str, ...]
    filings: tuple[DisclosureCandidate, ...]
    evidence: tuple[EvidenceSnippet, ...]
    mechanical_findings: tuple[str, ...]
    legal_assessment: str | None
    assessment_confidence: str
    amendment_status: str
    withdrawal_status: str
    effective_receipt_no: str | None
    relevance_reason: str
    schema_version: str = SCHEMA_VERSION


def to_dict(value: Any) -> dict[str, Any]:
    return asdict(value)
