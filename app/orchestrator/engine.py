"""Bounded Stage 1 search execution with channel fallback and evidence verification."""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from typing import Any
from typing import Callable

from app.channels.dart_fulltext import DartFulltextClient
from app.channels.opendart import ListCollection, OpenDartClient
from app.contracts import DisclosureCandidate, EvidenceSnippet, SearchExecutionDiagnostics, SearchRequest, VerifiedCase
from app.errors import ErrorCode, SearchError
from app.research.evidence import extract_evidence
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
        clock=time.monotonic,
    ):
        self.opendart = opendart
        self.dart = dart
        self.cache = cache or SessionTextCache()
        self.continuations = continuations or ContinuationStore()
        self.audit = audit
        self.company_resolver = company_resolver
        self.clock = clock
        self._known_candidates: dict[str, DisclosureCandidate] = {}

    def execute(self, request: SearchRequest) -> dict[str, Any]:
        lineage = _lineage(request)
        if not request.date_from or not request.date_to:
            return self._base_response(
                "clarification_required", lineage,
                warnings=["검색기간이 지정되지 않았습니다. 네트워크 검색 전에 시작일과 종료일을 확인해 주세요."],
                error={"code": ErrorCode.DATE_RANGE_REQUIRED.value, "message": "date_from과 date_to가 필요합니다."},
            )
        if request.exhaustive:
            return self._base_response(
                "batch_confirmation_required", lineage,
                warnings=["전수검색은 1단계 Fast Path에서 자동 실행하지 않습니다. 범위를 줄이거나 후속 배치 미리보기가 필요합니다."],
            )
        start = self.clock()
        plan = build_search_plan(request)
        diagnostics = SearchExecutionDiagnostics()
        warnings: list[str] = []
        candidates: list[DisclosureCandidate] = []
        list_result = ListCollection()
        from_date = date.fromisoformat(request.date_from)
        to_date = date.fromisoformat(request.date_to)
        continuation_state = None
        if request.continuation_token:
            continuation_state = self.continuations.consume(request.continuation_token, delete=True)
            if continuation_state.get("lineage") != lineage:
                raise SearchError(ErrorCode.INVALID_CONTINUATION_TOKEN, "다른 검색의 continuation token입니다.")

        fallback = False
        if plan.primary_channel == "dart_fulltext" and self.dart is not None:
            try:
                if not self.dart.health_check(diagnostics):
                    fallback = True
                    diagnostics.fallback_used = True
                    warnings.append("DART 본문검색 상태진단이 실패하여 OpenDART 원문검색으로 폴백합니다.")
                else:
                    candidates = self.dart.search_variants(
                        plan.query_variants, from_date, to_date, diagnostics,
                        request_budget=plan.dart_request_budget,
                        max_unique=plan.effective_document_budget,
                        company=request.company,
                    )
            except SearchError as exc:
                if exc.code in {ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN, ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED, ErrorCode.OPENDART_TEMPORARY_FAILURE}:
                    fallback = True
                    diagnostics.fallback_used = True
                    warnings.append(exc.message)
                else:
                    raise
        if plan.primary_channel == "opendart" or fallback or not candidates:
            if self.opendart is None:
                if candidates:
                    warnings.append("OpenDART API 키가 없어 후보 원문을 검증하지 못했습니다.")
                else:
                    return self._base_response(
                        "api_key_action_required", lineage, plan=plan, diagnostics=diagnostics,
                        warnings=["OpenDART API 키가 없어 목록·원문 검색을 실행할 수 없습니다."],
                        error={"code": ErrorCode.API_KEY_MISSING.value, "message": "DART_API_KEY를 설정해 주세요."},
                    )
            else:
                try:
                    corp_code = self._resolve_company(request.company, warnings)
                    list_result = self.opendart.collect_lists(
                        date_from=from_date, date_to=to_date, diagnostics=diagnostics,
                        request_budget=plan.list_request_budget, corp_code=corp_code,
                        start_window=int((continuation_state or {}).get("window", 0)),
                        start_page=int((continuation_state or {}).get("page", 1)),
                    )
                except SearchError as exc:
                    return self._channel_error_response(exc, lineage, plan, diagnostics, warnings, candidates)
                candidates = self._merge_candidates(candidates, list_result.candidates)

        # Global receipt-number dedupe happens before any document request.
        candidates = list({candidate.receipt_no: candidate for candidate in candidates}.values())
        verified: list[VerifiedCase] = []
        preliminary: list[DisclosureCandidate] = []
        processed_hashes = set((continuation_state or {}).get("processed_receipt_hashes", []))
        processed_this_run: list[str] = []
        listing_strategy = plan.strategy == "S1_company_disclosure_list"
        terminal_error: SearchError | None = None
        for candidate in candidates:
            receipt_hash = hashlib.sha256(candidate.receipt_no.encode()).hexdigest()
            if receipt_hash in processed_hashes:
                continue
            if len(verified) >= plan.result_budget:
                break
            processed_this_run.append(receipt_hash)
            if diagnostics.first_candidate_elapsed_ms is None:
                diagnostics.first_candidate_elapsed_ms = int((self.clock() - start) * 1000)
            if listing_strategy:
                evidence = EvidenceSnippet(candidate.receipt_no, f"{candidate.corp_name} | {candidate.report_name} | {candidate.receipt_date}", source="opendart_list", untrusted_source=False)
                finalized = replace(candidate, verification_status="verified", evidence=(evidence,))
                verified.append(self._to_case(finalized, request.query))
                self._known_candidates[finalized.receipt_no] = finalized
                continue
            if self.opendart is None:
                preliminary.append(candidate)
                continue
            text = self.cache.get(candidate.receipt_no)
            if text is not None:
                diagnostics.cache_hits += 1
            elif diagnostics.actual_document_requests < plan.effective_document_budget:
                requests_before = getattr(self.opendart, "requests_started", None)
                try:
                    text = self.opendart.download_document(candidate.receipt_no)
                    self.cache.put(candidate.receipt_no, text)
                except SearchError as exc:
                    if exc.code in {
                        ErrorCode.OPENDART_KEY_UNREGISTERED, ErrorCode.OPENDART_KEY_SUSPENDED,
                        ErrorCode.OPENDART_IP_NOT_ALLOWED, ErrorCode.OPENDART_REQUEST_LIMIT_EXCEEDED,
                        ErrorCode.OPENDART_SERVICE_MAINTENANCE, ErrorCode.OPENDART_PRIVACY_RETENTION_EXPIRED,
                    }:
                        terminal_error = exc
                        break
                    status = "document_unavailable" if exc.code == ErrorCode.OPENDART_FILE_NOT_FOUND else "parse_failed"
                    preliminary.append(replace(candidate, verification_status=status))
                    continue
                finally:
                    if requests_before is None:
                        diagnostics.actual_document_requests += 1
                    else:
                        diagnostics.actual_document_requests += self.opendart.requests_started - requests_before
            else:
                preliminary.append(candidate)
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

        diagnostics.completed_elapsed_ms = int((self.clock() - start) * 1000)
        pending = not list_result.complete or len(candidates) > len(verified) + len(preliminary) or diagnostics.actual_document_requests >= plan.effective_document_budget and bool(preliminary)
        token = None
        if not list_result.complete:
            token = self.continuations.issue({
                "lineage": lineage, "window": list_result.next_window_index, "page": list_result.next_page,
                "processed_receipt_hashes": [*processed_hashes, *processed_this_run],
            })
        elif pending:
            token = self.continuations.issue({
                "lineage": lineage, "window": 0, "page": 1, "reason": "document_budget",
                "processed_receipt_hashes": [*processed_hashes, *processed_this_run],
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
        }
        response = self._base_response(
            status, lineage, plan=plan, diagnostics=diagnostics, warnings=warnings,
            results=[_case_dict(case) for case in verified[: plan.result_budget]],
            preliminary=[_candidate_dict(candidate) for candidate in preliminary[: plan.preliminary_budget]],
            coverage=coverage,
            continuation_token=token,
            decision_summary=f"{plan.strategy}: {plan.primary_channel} 우선, 검증 원문 {diagnostics.actual_document_requests}건",
            error=response_error,
        )
        self._audit(request, response)
        return response

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

    def get_evidence(self, receipt_no: str, keywords: list[str], *, include_full_preview: bool = False, include_amendment_context: bool = True) -> dict[str, Any]:
        if not receipt_no.isdigit() or len(receipt_no) != defaults.RECEIPT_NO_LENGTH:
            raise ValueError(f"receipt_no must contain {defaults.RECEIPT_NO_LENGTH} digits")
        text = self.cache.get(receipt_no)
        if text is None:
            if self.opendart is None:
                raise SearchError(ErrorCode.API_KEY_MISSING, "근거 원문을 받으려면 OpenDART API 키가 필요합니다.")
            text = self.opendart.download_document(receipt_no)
            self.cache.put(receipt_no, text)
        evidence = extract_evidence(receipt_no, text, keywords)
        return {
            "receipt_no": receipt_no,
            "evidence": [asdict(item) | {"content_boundary": mark_untrusted(item.text)} for item in evidence],
            "include_full_preview": False,
            "full_preview_ignored": bool(include_full_preview),
            "amendment_context": "not_available_in_stage1_fast_path" if include_amendment_context else "not_requested",
            "dart_viewer_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}",
        }

    def _resolve_company(self, company: str | None, warnings: list[str]) -> str | None:
        if not company:
            return None
        if company.isdigit() and len(company) == defaults.CORP_CODE_LENGTH:
            return company
        if self.company_resolver is not None:
            resolved = self.company_resolver(company)
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
            effective_receipt_no=candidate.receipt_no,
            relevance_reason=f"'{query}' 검색의 기계적 원문/목록 근거",
        )

    @staticmethod
    def _base_response(status: str, lineage: str, **kwargs) -> dict[str, Any]:
        plan = kwargs.pop("plan", None)
        diagnostics = kwargs.pop("diagnostics", None)
        return {
            "status": status,
            "search_lineage_id": lineage,
            "schema_version": "1.0",
            "plan": asdict(plan) if plan else None,
            "coverage": kwargs.pop("coverage", {}),
            "diagnostics": asdict(diagnostics) if diagnostics else {},
            "warnings": kwargs.pop("warnings", []),
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
            "mode": request.mode,
            "date_from": request.date_from,
            "date_to": request.date_to,
            "company": request.company,
            "status": response["status"],
            "candidate_receipts": [item["case_id"] for item in response["results"]],
            "diagnostics": response["diagnostics"],
            "coverage": response["coverage"],
        })
