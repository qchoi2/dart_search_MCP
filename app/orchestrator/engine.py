"""Bounded Stage 1 search execution with channel fallback and evidence verification."""

from __future__ import annotations

import hashlib
import inspect
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from typing import Any
from typing import Callable

from app.channels.dart_fulltext import DartFulltextClient
from app.channels.adaptive import AdaptiveConcurrency
from app.channels.opendart import ListCollection, OpenDartClient
from app.contracts import DisclosureCandidate, EvidenceSnippet, SearchExecutionDiagnostics, SearchRequest, VerifiedCase
from app.errors import ErrorCode, SearchError
from app.http_client import DeadlineBudget
from app.research.evidence import extract_evidence
from app.research.normalization import dart_viewer_url
from app.security.untrusted_text import mark_untrusted
from app.storage.audit_log import AuditLog
from app.storage.continuation import ContinuationStore
from app.storage.session_cache import SessionTextCache
from app.config import defaults

from .plan_builder import build_search_plan


def _lineage(request: SearchRequest) -> str:
    normalized = "|".join((" ".join(request.query.casefold().split()), (request.company or "").casefold(), request.date_from or "", request.date_to or ""))
    return "search_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[: defaults.LINEAGE_HASH_CHARS]


def _candidate_dict(candidate: DisclosureCandidate) -> dict[str, Any]:
    result = asdict(candidate)
    result["evidence"] = [asdict(item) for item in candidate.evidence]
    return result


def _case_dict(case: VerifiedCase) -> dict[str, Any]:
    return asdict(case)


class SearchEngine:
    def __init__(
        self,
        *,
        opendart: OpenDartClient | None,
        dart: DartFulltextClient | None,
        cache: SessionTextCache | None = None,
        continuations: ContinuationStore | None = None,
        audit: AuditLog | None = None,
        company_resolver: Callable[[str], str | None] | None = None,
        list_concurrency: int = 1,
        document_concurrency: int = 1,
        clock=time.monotonic,
    ):
        self.opendart = opendart
        self.dart = dart
        self.cache = cache or SessionTextCache()
        self.continuations = continuations or ContinuationStore()
        self.audit = audit
        self.company_resolver = company_resolver
        self.list_concurrency = max(1, min(list_concurrency, defaults.LIST_CONCURRENCY))
        self.document_concurrency = max(1, min(document_concurrency, defaults.DOCUMENT_CONCURRENCY))
        self.clock = clock
        self._known_candidates: dict[str, DisclosureCandidate] = {}
        self._batch_recommendations: dict[str, tuple[float, int, int]] = {}

    def reset_session(self) -> None:
        """Explicit session reset; no server-expiry inference is performed."""
        if self.dart is not None:
            self.dart.reset_session()

    def execute(self, request: SearchRequest) -> dict[str, Any]:
        lineage = _lineage(request)
        if not request.date_from or not request.date_to:
            return self._base_response(
                "clarification_required", lineage,
                warnings=["검색기간이 지정되지 않았습니다. 네트워크 검색 전에 시작일과 종료일을 확인해 주세요."],
                error={"code": ErrorCode.DATE_RANGE_REQUIRED.value, "message": "date_from과 date_to가 필요합니다."},
            )
        if request.exhaustive or request.output_mode == "batch":
            return self._base_response(
                "batch_confirmation_required", lineage,
                warnings=["전수·배치 검색은 대화형 MCP에서 자동 실행하지 않습니다. 범위를 줄이거나 후속 배치 미리보기가 필요합니다."],
                batch_research_recommended=True,
                batch_recommendation_reason="exhaustive_or_batch_requested",
                batch_preview_tool="preview_batch_research",
            )
        start = self.clock()
        plan = build_search_plan(request)
        deadline = DeadlineBudget(start + plan.hard_timeout_seconds, clock=self.clock)
        diagnostics = SearchExecutionDiagnostics()
        warnings: list[str] = []
        warning_codes: list[str] = []
        warning_details: list[dict[str, Any]] = []
        candidates: list[DisclosureCandidate] = []
        list_result = ListCollection()
        hard_timeout = False
        from_date = date.fromisoformat(request.date_from)
        to_date = date.fromisoformat(request.date_to)
        continuation_state = None
        if request.continuation_token:
            continuation_state = self.continuations.consume(request.continuation_token)
            if continuation_state.get("lineage") != lineage:
                raise SearchError(ErrorCode.INVALID_CONTINUATION_TOKEN, "다른 검색의 continuation token입니다.")
            if continuation_state.get("date_from") not in {None, request.date_from} or continuation_state.get("date_to") not in {None, request.date_to}:
                raise SearchError(ErrorCode.INVALID_CONTINUATION_TOKEN, "continuation token의 검색기간이 현재 요청과 다릅니다.")
            stored_variants = continuation_state.get("query_variants")
            if stored_variants is not None and tuple(stored_variants) != plan.query_variants:
                raise SearchError(ErrorCode.INVALID_CONTINUATION_TOKEN, "continuation token의 검색어 변형 계약이 현재 계획과 다릅니다.")
            self.continuations.discard(request.continuation_token)

        resolved_company_code = None
        if request.company:
            try:
                resolved_company_code = self._resolve_company(request.company, warnings, deadline=deadline)
            except SearchError as exc:
                if exc.code == ErrorCode.SEARCH_TIMEOUT_PARTIAL:
                    hard_timeout = True
                    diagnostics.hard_timeout_reached = True
                else:
                    return self._channel_error_response(exc, lineage, plan, diagnostics, warnings, candidates)

        fallback = False
        disclosure_type = self._opendart_disclosure_type(request)
        if not hard_timeout and plan.primary_channel == "dart_fulltext" and self.dart is not None:
            try:
                if not self.dart.health_check(diagnostics, deadline=deadline):
                    fallback = True
                    diagnostics.fallback_used = True
                    message = "DART 본문검색 상태진단이 실패하여 OpenDART 원문검색으로 폴백합니다."
                    warnings.append(message)
                    self._add_fallback_warning(
                        warning_codes, warning_details, message=message,
                        reason=str(
                            getattr(getattr(self.dart, "breaker", None), "event", lambda: {})().get("failure_class")
                            or "status_diagnostic_failed"
                        ),
                        dart=self.dart,
                    )
                else:
                    candidates = self.dart.search_variants(
                        plan.query_variants, from_date, to_date, diagnostics,
                        request_budget=plan.dart_request_budget,
                        max_unique=plan.effective_document_budget,
                        company=resolved_company_code or request.company,
                        deadline=deadline,
                    )
                    candidates = self._apply_request_scope(candidates, request)
            except SearchError as exc:
                if exc.code == ErrorCode.SEARCH_TIMEOUT_PARTIAL:
                    hard_timeout = True
                    diagnostics.hard_timeout_reached = True
                elif exc.code in {
                    ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN,
                    ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED,
                    ErrorCode.OPENDART_TEMPORARY_FAILURE,
                    ErrorCode.OPENDART_HTTP_RATE_LIMITED,
                }:
                    fallback = True
                    diagnostics.fallback_used = True
                    warnings.append(exc.message)
                    reason = (exc.details or {}).get("failure_class") or (
                        "structure_or_access" if exc.code in {ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN, ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED}
                        else "network"
                    )
                    self._add_fallback_warning(
                        warning_codes, warning_details, message=exc.message,
                        reason=str(reason), dart=self.dart, error=exc,
                    )
                else:
                    raise
        if not hard_timeout and (
            plan.primary_channel == "opendart"
            or fallback
            or len(candidates) < plan.result_budget
            or disclosure_type is not None
        ):
            if self.opendart is None:
                if candidates:
                    warnings.append("OpenDART API 키가 없어 후보 원문을 검증하지 못했습니다.")
                else:
                    return self._base_response(
                        "api_key_action_required", lineage, plan=plan, diagnostics=diagnostics,
                        warnings=["OpenDART API 키가 없어 목록·원문 검색을 실행할 수 없습니다."],
                        warning_codes=warning_codes,
                        warning_details=warning_details,
                        completeness_grade="unconfirmed",
                        error={"code": ErrorCode.API_KEY_MISSING.value, "message": "DART_API_KEY를 설정해 주세요."},
                    )
            else:
                try:
                    list_result = self.opendart.collect_lists(
                        date_from=from_date, date_to=to_date, diagnostics=diagnostics,
                        request_budget=plan.list_request_budget, corp_code=resolved_company_code,
                        disclosure_type=disclosure_type,
                        start_window=int((continuation_state or {}).get("window", 0)),
                        start_page=int((continuation_state or {}).get("page", 1)),
                        deadline=deadline,
                        list_concurrency=self.list_concurrency,
                    )
                except SearchError as exc:
                    if exc.code == ErrorCode.SEARCH_TIMEOUT_PARTIAL:
                        hard_timeout = True
                        diagnostics.hard_timeout_reached = True
                        list_result.complete = False
                        list_result.next_window_index = int((continuation_state or {}).get("window", 0))
                        list_result.next_page = int((continuation_state or {}).get("page", 1))
                    else:
                        return self._channel_error_response(exc, lineage, plan, diagnostics, warnings, candidates)
                candidates = self._merge_candidates(candidates, list_result.candidates)
                candidates = self._apply_request_scope(candidates, request)

        # Global receipt-number dedupe happens before any document request.
        candidates = list({candidate.receipt_no: candidate for candidate in candidates}.values())
        verified: list[VerifiedCase] = []
        preliminary: list[DisclosureCandidate] = []
        processed_hashes = set((continuation_state or {}).get("processed_receipt_hashes", []))
        processed_this_run: list[str] = []
        listing_strategy = plan.strategy == "S1_company_disclosure_list"
        terminal_error: SearchError | None = None
        soft_timeout = False
        prefetch_errors: dict[str, SearchError] = {}
        prefetched_receipts: set[str] = set()
        adaptive_documents = AdaptiveConcurrency(self.document_concurrency)
        for candidate_index, candidate in enumerate(candidates):
            elapsed = self.clock() - start
            if deadline.remaining() <= 0:
                hard_timeout = True
                diagnostics.hard_timeout_reached = True
                break
            if elapsed >= plan.soft_timeout_seconds:
                diagnostics.soft_timeout_reached = True
                if len(verified) >= request.target_count and plan.strategy not in {"S5_event_sequence", "S6_amendment_comparison", "S7_effective_filing"}:
                    soft_timeout = True
                    break
            receipt_hash = hashlib.sha256(candidate.receipt_no.encode()).hexdigest()
            if receipt_hash in processed_hashes:
                continue
            if len(verified) >= plan.result_budget and plan.strategy not in {"S5_event_sequence", "S6_amendment_comparison", "S7_effective_filing"}:
                break
            if diagnostics.first_candidate_elapsed_ms is None:
                diagnostics.first_candidate_elapsed_ms = int((self.clock() - start) * 1000)
            if listing_strategy:
                evidence = EvidenceSnippet(candidate.receipt_no, f"{candidate.corp_name} | {candidate.report_name} | {candidate.receipt_date}", source="opendart_list", untrusted_source=False)
                finalized = replace(candidate, verification_status="verified", evidence=(evidence,))
                verified.append(self._to_case(finalized, request.query))
                self._known_candidates[finalized.receipt_no] = finalized
                processed_this_run.append(receipt_hash)
                continue
            if self.opendart is None:
                preliminary.append(candidate)
                processed_this_run.append(receipt_hash)
                continue
            text = self._cache_get(candidate.receipt_no, request.cache_mode)
            if text is not None:
                diagnostics.cache_hits += 1
            elif diagnostics.actual_document_requests < plan.effective_document_budget:
                if candidate.receipt_no not in prefetched_receipts:
                    remaining_budget = plan.effective_document_budget - diagnostics.actual_document_requests
                    batch: list[DisclosureCandidate] = []
                    for pending_candidate in candidates[candidate_index:]:
                        if len(batch) >= min(int(adaptive_documents.current or 1), remaining_budget):
                            break
                        pending_hash = hashlib.sha256(pending_candidate.receipt_no.encode()).hexdigest()
                        if pending_hash in processed_hashes or pending_candidate.receipt_no in prefetched_receipts:
                            continue
                        if self._cache_get(pending_candidate.receipt_no, request.cache_mode) is not None:
                            continue
                        batch.append(pending_candidate)
                    requests_before = getattr(self.opendart, "requests_started", None)
                    try:
                        with ThreadPoolExecutor(max_workers=int(adaptive_documents.current or 1)) as pool:
                            futures = {
                                item.receipt_no: pool.submit(self.opendart.download_document, item.receipt_no, deadline=deadline)
                                for item in batch
                            }
                            for receipt_no, future in futures.items():
                                prefetched_receipts.add(receipt_no)
                                try:
                                    downloaded = future.result()
                                except SearchError as exc:
                                    prefetch_errors[receipt_no] = exc
                                    adaptive_documents.observe(exc)
                                else:
                                    self._cache_put(receipt_no, downloaded, request.cache_mode)
                    finally:
                        if requests_before is None:
                            diagnostics.actual_document_requests += len(batch)
                        else:
                            diagnostics.actual_document_requests += self.opendart.requests_started - requests_before
                text = self._cache_get(candidate.receipt_no, request.cache_mode)
                try:
                    if candidate.receipt_no in prefetch_errors:
                        raise prefetch_errors[candidate.receipt_no]
                    if text is None:
                        raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "원문을 캐시에 적재하지 못했습니다.")
                except SearchError as exc:
                    if exc.code == ErrorCode.SEARCH_TIMEOUT_PARTIAL:
                        hard_timeout = True
                        diagnostics.hard_timeout_reached = True
                        break
                    if exc.code in {
                        ErrorCode.OPENDART_KEY_UNREGISTERED, ErrorCode.OPENDART_KEY_SUSPENDED,
                        ErrorCode.OPENDART_IP_NOT_ALLOWED, ErrorCode.OPENDART_REQUEST_LIMIT_EXCEEDED,
                        ErrorCode.OPENDART_SERVICE_MAINTENANCE, ErrorCode.OPENDART_PRIVACY_RETENTION_EXPIRED,
                    }:
                        terminal_error = exc
                        break
                    status = "document_unavailable" if exc.code == ErrorCode.OPENDART_FILE_NOT_FOUND else "parse_failed"
                    preliminary.append(replace(candidate, verification_status=status))
                    processed_this_run.append(receipt_hash)
                    continue
            else:
                preliminary.append(candidate)
                processed_this_run.append(receipt_hash)
                continue
            evidence = extract_evidence(candidate.receipt_no, text, plan.query_variants)
            if evidence:
                matched = tuple(dict.fromkeys(term for item in evidence for term in item.matched_terms))
                finalized = replace(
                    candidate,
                    verification_status="verified",
                    matched_terms=matched,
                    evidence=evidence[: defaults.EVIDENCE_PER_CASE],
                    source_channels=tuple(dict.fromkeys((*candidate.source_channels, "opendart_document"))),
                )
                verified.append(self._to_case(finalized, request.query))
                self._known_candidates[finalized.receipt_no] = finalized
            else:
                preliminary.append(replace(candidate, verification_status="excluded"))
            processed_this_run.append(receipt_hash)

        diagnostics.deadline_limited_timeout = deadline.deadline_limited_timeout
        diagnostics.deadline_request_start_blocked = deadline.request_start_blocked
        diagnostics.deadline_backoff_blocked = deadline.backoff_blocked
        diagnostics.processed_receipt_count = len(processed_this_run)
        diagnostics.unprocessed_candidate_count = max(0, len(candidates) - len(processed_hashes) - len(processed_this_run))
        diagnostics.completed_elapsed_ms = int((self.clock() - start) * 1000)
        pending = hard_timeout or soft_timeout or not list_result.complete or len(candidates) > len(verified) + len(preliminary) or diagnostics.actual_document_requests >= plan.effective_document_budget and bool(preliminary)
        token = None
        continuation_reason = None
        continuation_base = {
            "lineage": lineage,
            "date_from": request.date_from,
            "date_to": request.date_to,
            "query_variants": list(plan.query_variants),
            "processed_receipt_hashes": [*processed_hashes, *processed_this_run],
        }
        if not list_result.complete:
            continuation_reason = "list_request_budget"
            token = self.continuations.issue({
                **continuation_base,
                "window": list_result.next_window_index,
                "page": list_result.next_page,
                "reason": continuation_reason,
            })
        elif pending:
            continuation_reason = "hard_timeout" if hard_timeout else "soft_timeout" if soft_timeout else "document_budget"
            token = self.continuations.issue({
                **continuation_base,
                "window": 0,
                "page": 1,
                "reason": continuation_reason,
            })
        status = "partial" if token else "completed"
        response_error = None
        if terminal_error is not None:
            status = self._response_status_for_error(terminal_error, has_results=bool(verified))
            response_error = terminal_error.to_dict()
            warnings.append(terminal_error.message)
        elif self.opendart is None and candidates and not listing_strategy:
            status = "api_key_action_required"
            response_error = {"code": ErrorCode.API_KEY_MISSING.value, "message": "후보 원문 검증을 위해 DART_API_KEY를 설정해 주세요."}
        elif hard_timeout:
            response_error = {"code": ErrorCode.SEARCH_TIMEOUT_PARTIAL.value, "message": "하드 시간예산에 도달해 부분 결과와 continuation token을 반환합니다."}
            warnings.append(response_error["message"])
            self._add_warning(
                warning_codes, warning_details, ErrorCode.SEARCH_TIMEOUT_PARTIAL.value,
                response_error["message"],
                processed_receipts=diagnostics.processed_receipt_count,
                unprocessed_candidates=diagnostics.unprocessed_candidate_count,
                deadline_limited_timeout=diagnostics.deadline_limited_timeout,
            )
        if token and not hard_timeout:
            message = "대화형 검색예산 안에서 완료하지 못한 범위를 continuation token으로 남겼습니다."
            warnings.append(message)
            self._add_warning(
                warning_codes,
                warning_details,
                "SEARCH_BUDGET_PARTIAL",
                message,
                reason=continuation_reason,
                processed_receipts=diagnostics.processed_receipt_count,
                unprocessed_candidates=diagnostics.unprocessed_candidate_count,
            )
        if diagnostics.latest_first_bias:
            message = "DART 최신순 상위 결과만 확인되어 오래된 사례가 포함되지 않았을 수 있습니다."
            warnings.append(message)
            self._add_warning(
                warning_codes,
                warning_details,
                "LATEST_FIRST_BIAS",
                message,
                fully_pageable_by_query=diagnostics.fully_pageable_by_query,
            )
        if not verified and not preliminary and not token:
            warnings.append("지정한 범위에서 정상적으로 검색했지만 확인된 결과가 없습니다.")
        coverage = {
            "date_from": request.date_from,
            "date_to": request.date_to,
            "complete": token is None,
            "server_sort": "date_desc",
            "local_ranking": "mechanical_score",
            "latest_first_bias": diagnostics.latest_first_bias,
            "fallback_used": fallback,
            "actual_document_verification_count": diagnostics.actual_document_requests,
            "unprocessed_candidate_count": diagnostics.unprocessed_candidate_count,
            "processed_window_count": diagnostics.processed_window_count,
            "remaining_scope": {
                "next_window_index": list_result.next_window_index if not list_result.complete else None,
                "next_page": list_result.next_page if not list_result.complete else None,
            },
        }
        if diagnostics.pagination_contract_changed:
            message = "DART 페이지 계산이 실측 10행 계약과 달라 전체 검색 범위를 확정할 수 없습니다."
            warnings.append(message)
            self._add_warning(
                warning_codes,
                warning_details,
                "PAGINATION_CONTRACT_CHANGED",
                message,
                observations=diagnostics.pagination_contract_observations,
            )
        completeness_grade = self._completeness_grade(
            status=status,
            fallback=fallback,
            has_continuation=token is not None,
            latest_first_bias=diagnostics.latest_first_bias,
            pagination_contract_changed=diagnostics.pagination_contract_changed,
        )
        for detail in warning_details:
            if detail.get("code") == "DART_FULLTEXT_FALLBACK":
                detail["actual_document_verification_count"] = diagnostics.actual_document_requests
                detail["unprocessed_candidate_count"] = diagnostics.unprocessed_candidate_count
        relation_analysis, grouped_verified = self._relation_analysis(plan.strategy, verified, candidates)
        response = self._base_response(
            status, lineage, plan=plan, diagnostics=diagnostics, warnings=warnings,
            warning_codes=warning_codes, warning_details=warning_details,
            completeness_grade=completeness_grade,
            results=[_case_dict(case) for case in grouped_verified[: plan.result_budget]],
            preliminary=[_candidate_dict(candidate) for candidate in preliminary[: plan.preliminary_budget]],
            coverage=coverage,
            continuation_token=token,
            decision_summary=f"{plan.strategy}: {plan.primary_channel} 우선, 검증 원문 {diagnostics.actual_document_requests}건",
            error=response_error,
            relation_analysis=relation_analysis,
        )
        response.update(
            self._batch_recommendation(
                lineage=hashlib.sha256(
                    "|".join((" ".join(request.query.casefold().split()), (request.company or "").casefold())).encode()
                ).hexdigest(),
                plan=plan,
                diagnostics=diagnostics,
                candidate_count=len(candidates),
                fallback=fallback,
            )
        )
        self._audit(request, response)
        return response

    def _relation_analysis(
        self,
        strategy: str,
        verified: list[VerifiedCase],
        relation_candidates: list[DisclosureCandidate] | None = None,
    ) -> tuple[dict[str, Any] | None, list[VerifiedCase]]:
        if strategy not in {"S5_event_sequence", "S6_amendment_comparison", "S7_effective_filing"}:
            return None, verified
        from app.research.amendments import build_amendment_chains, compare_amendment_chain, extract_amendment_context
        from app.research.events import build_event_graph
        verified_candidates = [case.filings[0] for case in verified if case.filings]
        candidates = list({
            candidate.receipt_no: candidate
            for candidate in (relation_candidates or verified_candidates)
            if self._cache_get(candidate.receipt_no, "auto") is not None
        }.values())
        texts = {
            candidate.receipt_no: text
            for candidate in candidates
            if (text := self._cache_get(candidate.receipt_no, "auto")) is not None
        }
        if strategy == "S5_event_sequence":
            return {"strategy": strategy, "event_graph": build_event_graph(candidates, texts)}, verified
        contexts = {
            candidate.receipt_no: extract_amendment_context(texts.get(candidate.receipt_no, ""), receipt_no=candidate.receipt_no)
            for candidate in candidates
        }
        chains = build_amendment_chains(candidates, contexts)
        comparisons = [compare_amendment_chain(chain, texts, contexts) for chain in chains]
        case_by_receipt = {case.case_id: case for case in verified}
        candidate_by_receipt = {candidate.receipt_no: candidate for candidate in candidates}
        grouped: list[VerifiedCase] = []
        consumed: set[str] = set()
        for chain in chains:
            receipts = chain["member_receipt_nos"]
            if len(receipts) < 2:
                continue
            cases = [case_by_receipt[receipt] for receipt in receipts if receipt in case_by_receipt]
            if not cases:
                continue
            consumed.update(receipts)
            filings = tuple(candidate_by_receipt[receipt] for receipt in receipts if receipt in candidate_by_receipt)
            evidence = tuple(item for case in cases for item in case.evidence)[: defaults.EVIDENCE_MAX_SNIPPETS]
            grouped.append(VerifiedCase(
                case_id=chain["amendment_chain_id"],
                case_title=cases[-1].case_title,
                companies=tuple(dict.fromkeys(company for case in cases for company in case.companies)),
                filings=filings,
                evidence=evidence,
                mechanical_findings=(
                    f"명시 관계 근거로 정정 체인 {len(filings)}건 연결",
                    "원공시↔최종본 구조 비교 수행",
                ),
                legal_assessment=None,
                assessment_confidence="not_assessed",
                amendment_status="linked_confirmed" if chain["chain_confidence"] == "confirmed" else "linked_uncertain",
                withdrawal_status=chain["withdrawal_status"],
                effective_receipt_no=chain["effective_receipt_no"],
                relevance_reason="S6/S7 온디맨드 정정 관계 분석",
            ))
        grouped.extend(case for case in verified if case.case_id not in consumed)
        analysis = {
            "strategy": strategy,
            "amendment_chains": chains,
            "comparisons": comparisons,
            "uncertain_chain_count": sum(1 for chain in chains if chain["chain_confidence"] == "uncertain"),
            "confirmed_chain_count": sum(1 for chain in chains if chain["chain_confidence"] == "confirmed"),
        }
        return analysis, grouped

    def _batch_recommendation(
        self,
        *,
        lineage: str,
        plan,
        diagnostics: SearchExecutionDiagnostics,
        candidate_count: int,
        fallback: bool,
    ) -> dict[str, Any]:
        estimated_dart_rows = sum(
            max(0, int(last_page or 0)) * defaults.DART_EFFECTIVE_PAGE_SIZE
            for last_page in diagnostics.dart_linked_last_page_by_query.values()
        )
        filtered_documents = max(candidate_count, int(estimated_dart_rows * 0.65))
        dart_rate_floor = max(
            0,
            diagnostics.dart_result_page_requests + diagnostics.mode_setup_requests - 1,
        ) * defaults.DART_MIN_REQUEST_INTERVAL_SECONDS
        estimated_seconds = int(max(dart_rate_floor, filtered_documents * 1.05))
        thresholds = dict(plan.batch_threshold)
        reason = None
        if filtered_documents > thresholds["estimated_documents"]:
            reason = "filtered_estimated_documents_exceed_threshold"
        elif estimated_seconds > thresholds["estimated_seconds"]:
            reason = "estimated_duration_exceeds_threshold"
        elif fallback and filtered_documents >= max(1, int(plan.effective_document_budget * 1.25)):
            reason = "fallback_estimate_increased"
        if reason is None:
            return {
                "batch_research_recommended": False,
                "batch_recommendation_reason": None,
                "batch_estimate": {
                    "filtered_estimated_documents": filtered_documents,
                    "estimated_seconds": estimated_seconds,
                },
            }
        now = self.clock()
        previous = self._batch_recommendations.get(lineage)
        if previous and now - previous[0] < defaults.CONTINUATION_TTL_SECONDS:
            old_documents, old_seconds = previous[1], previous[2]
            materially_larger = (
                filtered_documents >= max(1, int(old_documents * 1.5))
                or estimated_seconds >= max(1, int(old_seconds * 1.5))
            )
            if not materially_larger:
                return {
                    "batch_research_recommended": False,
                    "batch_recommendation_suppressed": True,
                    "batch_recommendation_reason": reason,
                    "batch_estimate": {
                        "filtered_estimated_documents": filtered_documents,
                        "estimated_seconds": estimated_seconds,
                    },
                }
        self._batch_recommendations[lineage] = (now, filtered_documents, estimated_seconds)
        return {
            "batch_research_recommended": True,
            "batch_recommendation_suppressed": False,
            "batch_recommendation_reason": reason,
            "batch_preview_tool": "preview_batch_research",
            "batch_estimate": {
                "filtered_estimated_documents": filtered_documents,
                "estimated_seconds": estimated_seconds,
            },
        }

    @staticmethod
    def _response_status_for_error(error: SearchError, *, has_results: bool) -> str:
        if error.code in {
            ErrorCode.OPENDART_KEY_UNREGISTERED, ErrorCode.OPENDART_KEY_SUSPENDED,
            ErrorCode.OPENDART_IP_NOT_ALLOWED, ErrorCode.OPENDART_PRIVACY_RETENTION_EXPIRED,
        }:
            return "api_key_action_required"
        if has_results and error.code in {ErrorCode.OPENDART_REQUEST_LIMIT_EXCEEDED, ErrorCode.OPENDART_SERVICE_MAINTENANCE, ErrorCode.OPENDART_TEMPORARY_FAILURE}:
            return "partial"
        return "failed"

    def _channel_error_response(
        self,
        error: SearchError,
        lineage: str,
        plan,
        diagnostics: SearchExecutionDiagnostics,
        warnings: list[str],
        candidates: list[DisclosureCandidate],
    ) -> dict[str, Any]:
        status = self._response_status_for_error(error, has_results=bool(candidates))
        return self._base_response(
            status, lineage, plan=plan, diagnostics=diagnostics,
            warnings=[*warnings, error.message], error=error.to_dict(),
            preliminary=[_candidate_dict(item) for item in candidates[: plan.preliminary_budget]],
            coverage={"complete": False, "fallback_used": diagnostics.fallback_used},
            decision_summary="OpenDART 상태코드 정책에 따라 재시도 없이 중단",
        )

    @staticmethod
    def _add_warning(
        codes: list[str],
        details: list[dict[str, Any]],
        code: str,
        message: str,
        **extra: Any,
    ) -> None:
        if code not in codes:
            codes.append(code)
        if not any(item.get("code") == code for item in details):
            details.append({"code": code, "message": message, **extra})

    @classmethod
    def _add_fallback_warning(
        cls,
        codes: list[str],
        details: list[dict[str, Any]],
        *,
        message: str,
        reason: str,
        dart: DartFulltextClient,
        error: SearchError | None = None,
    ) -> None:
        breaker = getattr(dart, "breaker", None)
        event = breaker.event() if breaker is not None else {}
        blocked = int((error.details or {}).get("blocked_seconds", 0)) if error else 0
        if event.get("status") == "CIRCUIT_OPEN":
            blocked = max(
                1,
                blocked,
                breaker.remaining_blocked_seconds(),
            )
        else:
            blocked = 0
        cls._add_warning(
            codes, details, "DART_FULLTEXT_FALLBACK", message,
            reason=reason,
            fallback_source=str((error.details or {}).get("fallback_source", "opendart_document_search")) if error else "opendart_document_search",
            blocked_seconds=blocked,
        )

    @staticmethod
    def _completeness_grade(
        *,
        status: str,
        fallback: bool,
        has_continuation: bool,
        latest_first_bias: bool,
        pagination_contract_changed: bool = False,
    ) -> str:
        if status in {"failed", "api_key_action_required"}:
            return "unconfirmed"
        if pagination_contract_changed:
            return "unconfirmed"
        if status == "partial" or has_continuation:
            return "partial"
        if fallback or latest_first_bias:
            return "reduced"
        return "complete"

    def get_evidence(self, receipt_no: str, keywords: list[str], *, include_full_preview: bool = False, include_amendment_context: bool = True) -> dict[str, Any]:
        if not isinstance(receipt_no, str) or not receipt_no.isdigit() or len(receipt_no) != defaults.RECEIPT_NO_LENGTH:
            raise ValueError(f"receipt_no must contain {defaults.RECEIPT_NO_LENGTH} digits")
        if not isinstance(keywords, list) or not keywords or len(keywords) > defaults.INTERACTIVE_TARGET_MAX:
            raise ValueError(f"keywords must contain 1 to {defaults.INTERACTIVE_TARGET_MAX} strings")
        if any(not isinstance(keyword, str) or not keyword.strip() or len(keyword) > defaults.QUERY_MAX_CHARS for keyword in keywords):
            raise ValueError(f"each keyword must contain 1 to {defaults.QUERY_MAX_CHARS} characters")
        normalized_keywords = [keyword.strip() for keyword in keywords]
        if len(set(normalized_keywords)) != len(normalized_keywords):
            raise ValueError("keywords must not contain duplicates")
        if not isinstance(include_full_preview, bool) or not isinstance(include_amendment_context, bool):
            raise ValueError("evidence include flags must be boolean")
        text = self._cache_get(receipt_no, "auto")
        if text is None:
            if self.opendart is None:
                raise SearchError(ErrorCode.API_KEY_MISSING, "근거 원문을 받으려면 OpenDART API 키가 필요합니다.")
            text = self.opendart.download_document(receipt_no)
            self._cache_put(receipt_no, text, "auto")
        evidence = extract_evidence(receipt_no, text, normalized_keywords)
        evidence_payload = []
        for item in evidence[: defaults.EVIDENCE_MAX_SNIPPETS]:
            boundary = mark_untrusted(item.text)
            boundary.pop("content", None)
            evidence_payload.append(asdict(item) | {"content_boundary": boundary})
        return {
            "status": "completed",
            "schema_version": defaults.SCHEMA_VERSION,
            "receipt_no": receipt_no,
            "evidence": evidence_payload,
            "evidence_count": len(evidence_payload),
            "source_text_untrusted": True,
            "include_full_preview": False,
            "full_preview_ignored": bool(include_full_preview),
            "amendment_context": self._amendment_context_payload(text, receipt_no) if include_amendment_context else "not_requested",
            "dart_viewer_url": dart_viewer_url(receipt_no),
        }

    @staticmethod
    def _amendment_context_payload(text: str, receipt_no: str) -> dict[str, Any]:
        from app.research.amendments import extract_amendment_context

        return asdict(extract_amendment_context(text, receipt_no=receipt_no))

    def _cache_get(self, receipt_no: str, cache_mode: str) -> str | None:
        if cache_mode == "session" and hasattr(self.cache, "get_session"):
            return self.cache.get_session(receipt_no)
        return self.cache.get(receipt_no)

    def _cache_put(self, receipt_no: str, text: str, cache_mode: str) -> None:
        if cache_mode == "session" and hasattr(self.cache, "put_session"):
            self.cache.put_session(receipt_no, text)
        else:
            self.cache.put(receipt_no, text)

    def _resolve_company(self, company: str | None, warnings: list[str], *, deadline: DeadlineBudget | None = None) -> str | None:
        if not company:
            return None
        if company.isdigit() and len(company) == defaults.CORP_CODE_LENGTH:
            return company
        if self.company_resolver is not None:
            parameters = inspect.signature(self.company_resolver).parameters
            resolved = self.company_resolver(company, deadline=deadline) if "deadline" in parameters else self.company_resolver(company)
            if resolved:
                return resolved
        # Name resolution is intentionally deferred to the cached company directory in the MCP factory.
        warnings.append("회사명이 고유번호가 아니어서 DART 본문검색 회사필터만 적용했습니다. OpenDART 목록은 시장범위로 검증합니다.")
        return None

    @staticmethod
    def _merge_candidates(left: list[DisclosureCandidate], right: list[DisclosureCandidate]) -> list[DisclosureCandidate]:
        result = {candidate.receipt_no: candidate for candidate in left}
        order = [candidate.receipt_no for candidate in left]
        for candidate in right:
            previous = result.get(candidate.receipt_no)
            if previous is None:
                order.append(candidate.receipt_no)
                result[candidate.receipt_no] = candidate
            else:
                result[candidate.receipt_no] = replace(
                    candidate,
                    source_channels=tuple(dict.fromkeys((*previous.source_channels, *candidate.source_channels))),
                    matched_terms=previous.matched_terms,
                    fulltext_match_scope=previous.fulltext_match_scope,
                    fulltext_row_tags=previous.fulltext_row_tags,
                    mechanical_score=previous.mechanical_score,
                )
        return [result[key] for key in order]

    @staticmethod
    def _apply_request_scope(candidates: list[DisclosureCandidate], request: SearchRequest) -> list[DisclosureCandidate]:
        normalized_query = "".join(request.query.split())
        if "주요사항보고서" in normalized_query:
            return [
                candidate
                for candidate in candidates
                if "".join(candidate.report_name.split()).startswith("주요사항보고서")
            ]
        return candidates

    @staticmethod
    def _opendart_disclosure_type(request: SearchRequest) -> str | None:
        normalized_query = "".join(request.query.split())
        if "주요사항보고서" in normalized_query:
            return "B"
        return None

    @staticmethod
    def _to_case(candidate: DisclosureCandidate, query: str) -> VerifiedCase:
        withdrawal = "candidate_signal_only" if "철" in candidate.rm_flags else "not_indicated"
        amendment = "prefix_present_unlinked" if candidate.report_name_prefixes else "not_indicated"
        findings = [f"원문에서 검색어 확인: {', '.join(candidate.matched_terms)}"] if candidate.matched_terms else ["OpenDART 목록 메타데이터 확인"]
        return VerifiedCase(
            case_id=candidate.receipt_no,
            case_title=f"{candidate.corp_name} - {candidate.report_name}",
            companies=(candidate.corp_name,),
            filings=(candidate,),
            evidence=candidate.evidence,
            mechanical_findings=tuple(findings),
            legal_assessment=None,
            assessment_confidence="not_assessed",
            amendment_status=amendment,
            withdrawal_status=withdrawal,
            effective_receipt_no=None,
            relevance_reason=f"'{query}' 검색의 기계적 원문/목록 근거",
        )

    @staticmethod
    def _base_response(status: str, lineage: str, **kwargs) -> dict[str, Any]:
        plan = kwargs.pop("plan", None)
        diagnostics = kwargs.pop("diagnostics", None)
        non_executed_statuses = {
            "failed",
            "clarification_required",
            "batch_confirmation_required",
            "continuation_confirmation_required",
            "api_key_action_required",
        }
        return {
            "status": status,
            "search_lineage_id": lineage,
            "schema_version": "1.0",
            "plan": asdict(plan) if plan else None,
            "coverage": kwargs.pop("coverage", {"complete": status == "completed"}),
            "diagnostics": asdict(diagnostics) if diagnostics else {},
            "actual_document_verification_count": diagnostics.actual_document_requests if diagnostics else 0,
            "unprocessed_candidate_count": diagnostics.unprocessed_candidate_count if diagnostics else 0,
            "warnings": kwargs.pop("warnings", []),
            "warning_codes": kwargs.pop("warning_codes", []),
            "warning_details": kwargs.pop("warning_details", []),
            "completeness_grade": kwargs.pop("completeness_grade", "unconfirmed" if status in non_executed_statuses else "complete"),
            "decision_summary": kwargs.pop("decision_summary", "검색 전 필수조건 확인"),
            "results": kwargs.pop("results", []),
            "preliminary_candidates": kwargs.pop("preliminary", []),
            "continuation_token": kwargs.pop("continuation_token", None),
            **kwargs,
        }

    def _audit(self, request: SearchRequest, response: dict[str, Any]) -> None:
        if self.audit is None:
            return
        self.audit.append_summary({
            "ts": datetime.now(timezone.utc).isoformat(),
            "search_lineage_id": response["search_lineage_id"],
            "normalized_query_hash": hashlib.sha256(" ".join(request.query.casefold().split()).encode()).hexdigest(),
            "executed_query_variants": (response.get("plan") or {}).get("query_variants", []),
            "search_period": {"date_from": request.date_from, "date_to": request.date_to},
            "scope": {"company": request.company, "disclosure_type": self._opendart_disclosure_type(request)},
            "candidate_receipts": list(dict.fromkeys([
                *[item["case_id"] for item in response["results"]],
                *[item["receipt_no"] for item in response["preliminary_candidates"]],
            ])),
            "verified_receipts": [item["case_id"] for item in response["results"]],
            "exclusion_reasons": [
                {"receipt_no": item["receipt_no"], "reason": item["verification_status"]}
                for item in response["preliminary_candidates"]
                if item["verification_status"] != "unverified"
            ],
            "call_cache_retry_diagnostics": response["diagnostics"],
            "warning_codes": response.get("warning_codes", []),
            "completeness_grade": response.get("completeness_grade"),
        })
