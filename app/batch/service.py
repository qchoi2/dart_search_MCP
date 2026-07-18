from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app.channels.dart_fulltext import dart_date_windows, row_to_candidate
from app.channels.adaptive import AdaptiveConcurrency
from app.channels.opendart import candidate_from_list_row, split_date_windows
from app.config import Settings, defaults
from app.errors import ErrorCode, SearchError
from app.http_client import DeadlineBudget
from app.contracts import ChannelStatus, SearchExecutionDiagnostics, SearchRequest
from app.orchestrator.plan_builder import decompose_query, resolve_query_terms, title_constraint
from app.research.evidence import extract_cooccurrence_evidence, extract_evidence
from app.research.normalization import dart_viewer_url
from app.security.csv_guard import escape_csv_cell, has_formula_prefix
from app.storage.atomic import atomic_write_bytes
from app.storage.batch_store import BatchCheckpointStore, BatchPlanStore, BatchResultStore


INTERVAL_OPTIONS = (5, 10, 15, 30)


def _stable_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class BatchResearchService:
    def __init__(
        self,
        engine: Any,
        *,
        root: Path,
        settings: Settings | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        plans: BatchPlanStore | None = None,
    ) -> None:
        self.engine = engine
        self.opendart = getattr(engine, "opendart", None)
        self.dart = getattr(engine, "dart", None)
        self.settings = settings or Settings(defaults.DEFAULT_SETTINGS)
        self.clock = clock
        self.wall_clock = wall_clock
        self.plans = plans or BatchPlanStore(clock=wall_clock)
        retention = int(self.settings.get("batch.checkpoint_retention_days", defaults.CHECKPOINT_RETENTION_DAYS))
        self.checkpoints = BatchCheckpointStore(root / "checkpoints", retention_days=retention)
        self.records = BatchResultStore(root / "records", retention_days=1)

    def preview(
        self,
        *,
        query: str,
        date_from: date,
        date_to: date,
        company: str | None = None,
        disclosure_types: Iterable[str] = (),
        target_count: int = 500,
        exhaustive: bool = True,
    ) -> dict[str, Any]:
        self.checkpoints.cleanup(now=self.wall_clock())
        self.records.cleanup(now=self.wall_clock())
        disclosure_scope = list(disclosure_types)
        resolved_company_code: str | None = None
        if company:
            if company.isdigit() and len(company) == defaults.CORP_CODE_LENGTH:
                resolved_company_code = company
            else:
                resolver = getattr(self.engine, "company_resolver", None)
                if resolver is not None:
                    try:
                        resolved_company_code = resolver(company)
                    except (SearchError, TypeError, ValueError):
                        resolved_company_code = None
                if not resolved_company_code:
                    return {
                        "status": "company_resolution_required",
                        "company": company,
                        "document_download_started": False,
                    }
        request = SearchRequest(
            query=query,
            company=company,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
            target_count=target_count,
            exhaustive=exhaustive,
            output_mode="batch",
        )
        variants, verification_groups = resolve_query_terms(query)
        search_mode = "contents"
        constraint = title_constraint(query)
        if constraint is not None and constraint.get("opendart_detail_type"):
            # Report-class scope: the OpenDART list (pblntf_detail_ty) shrinks
            # the pool to the named class; every body concept (including the
            # trigger) then verifies by co-occurrence. The DART report(title)
            # mode stays dormant until a live probe shows it returning rows.
            if not disclosure_scope:
                disclosure_scope = [constraint["opendart_detail_type"]]
            if not verification_groups:
                _, verification_groups = decompose_query(query)
        variants = list(variants)
        verification_groups = [list(group) for group in verification_groups]
        scope = {
            "query_hash": hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest(),
            "company": company,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "disclosure_types": disclosure_scope,
            "target_count": target_count,
            "exhaustive": exhaustive,
        }
        lineage = _stable_hash(
            {
                "query_hash": scope["query_hash"],
                "company": company,
                "disclosure_types": scope["disclosure_types"],
            }
        )
        scope_signature = _stable_hash(scope)
        scope_weight = max(1, (date_to - date_from).days + 1) * target_count
        existing = self.plans.lookup(
            lineage=lineage,
            scope_signature=scope_signature,
            scope_weight=scope_weight,
        )
        if existing is not None:
            return existing
        windows = list(reversed(split_date_windows(date_from, date_to)))
        estimate = self._estimate(request, variants, windows, disclosure_scope, resolved_company_code, search_mode)
        payload = {
            "status": "confirmation_required",
            "feature": "deep_search",
            "feature_label": "공시 MCP의 심화 검색기능",
            "guidance": "예상 범위와 시간을 확인한 뒤 심화 검색을 시작할 수 있습니다.",
            "help": "심화 검색기능이 무엇인지 궁금하면 물어봐 주세요.",
            "scope": scope,
            "scope_signature": scope_signature,
            "scope_weight": scope_weight,
            "request": {
                "query": query,
                "company": company,
                "resolved_company_code": resolved_company_code,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "disclosure_types": disclosure_scope,
                "target_count": target_count,
                "exhaustive": exhaustive,
            },
            "dart_query": {"variants": variants, "window_count": len(windows), "mode": search_mode, "verification_term_groups": verification_groups},
            **estimate,
            "output": ["CSV", "JSON"],
            "cache_policy": "temporary_metadata_evidence_ttl_24h",
            "checkpointing": "date_window_page_and_row",
            "retention": {
                "checkpoint_days": int(self.settings.get("batch.checkpoint_retention_days", defaults.CHECKPOINT_RETENTION_DAYS)),
                "result_record_hours": 24,
                "raw_document_storage": "disabled",
                "deletion": "completed_checkpoints_immediately; expired_artifacts_on_next_batch_operation",
            },
            "confirmation_interval_options_minutes": list(INTERVAL_OPTIONS),
            "recommended_confirmation_interval_minutes": int(self.settings.get("batch.recommended_confirmation_minutes", defaults.BATCH_RECOMMENDED_CONFIRMATION_MINUTES)),
            "selected_confirmation_interval_minutes": None,
        }
        plan, _ = self.plans.issue(lineage=lineage, payload=payload)
        return plan

    def run(
        self,
        *,
        plan_id: str,
        approved: bool,
        confirmation_interval_minutes: int | None = None,
    ) -> dict[str, Any]:
        self.checkpoints.cleanup(now=self.wall_clock())
        plan = self.plans.get(plan_id)
        if plan is None:
            return {"status": "invalid_or_expired_plan", "network_started": False}
        if not approved:
            self.plans.decline(plan_id)
            return {
                "status": "declined",
                "network_started": False,
                "recommendation_suppressed_seconds": 30 * 60,
            }
        interval_error = self._validate_interval(confirmation_interval_minutes)
        if interval_error:
            return interval_error
        plan = self.plans.consume(plan_id)
        assert plan is not None
        job_id = self.checkpoints.new_id("job")
        state = self._new_state(job_id, plan)
        self.checkpoints.save(job_id, state)
        return self._execute_segment(state, int(confirmation_interval_minutes))

    def continue_run(
        self,
        *,
        job_id: str,
        approved: bool,
        confirmation_interval_minutes: int | None = None,
    ) -> dict[str, Any]:
        self.checkpoints.cleanup(now=self.wall_clock())
        try:
            state = self.checkpoints.load(job_id)
        except ValueError:
            state = None
        if state is None:
            return {"status": "invalid_or_expired_checkpoint", "network_started": False}
        if not approved:
            return {"status": "continuation_declined", "network_started": False, "job_id": job_id}
        interval_error = self._validate_interval(confirmation_interval_minutes)
        if interval_error:
            interval_error["job_id"] = job_id
            return interval_error
        self._restore_circuit(state.get("circuit_state"))
        return self._execute_segment(state, int(confirmation_interval_minutes))

    def export(
        self,
        *,
        search_record_id: str,
        formats: Iterable[str],
        output_directory: str | None,
    ) -> dict[str, Any]:
        self.records.cleanup(now=self.wall_clock())
        if not output_directory:
            return {
                "status": "clarification_required",
                "message": "output_directory를 지정해 주세요.",
                "files_written": [],
            }
        requested = [str(value).lower() for value in formats]
        if not requested or any(value not in {"csv", "json"} for value in requested):
            return {"status": "invalid_formats", "allowed_formats": ["csv", "json"], "files_written": []}
        try:
            record = self.records.load(search_record_id)
        except ValueError:
            record = None
        if record is None:
            return {"status": "record_not_found", "files_written": []}
        target = Path(output_directory).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for format_name in dict.fromkeys(requested):
            path = target / f"{search_record_id}.{format_name}"
            if format_name == "json":
                raw = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
            else:
                raw = self._csv_bytes(record.get("results", []))
            atomic_write_bytes(path, raw)
            written.append(str(path))
        return {"status": "completed", "search_record_id": search_record_id, "files_written": written}

    def _estimate(
        self,
        request: SearchRequest,
        variants: list[str],
        windows: list[Any],
        disclosure_types: list[str],
        resolved_company_code: str | None,
        mode: str = "contents",
    ) -> dict[str, Any]:
        # Preview is deliberately metadata-only: no disclosure document is downloaded.
        type_scopes: list[str | None] = disclosure_types or [None]
        opendart_available = self.opendart is not None and hasattr(self.opendart, "list_page")
        # A report-class scope (e.g. D004) runs the deep search on the scoped
        # OpenDART list only; the DART body channel is idle, so it must not
        # inflate the estimate with unscoped whole-market keyword hits.
        dart_available = self.dart is not None and hasattr(self.dart, "search_page") and not disclosure_types
        estimated_list_requests = len(windows) * len(type_scopes) if opendart_available else 0
        estimated_dart_requests = len(windows) * max(1, len(variants)) if dart_available else 0
        observed_list_rows = 0
        observed_dart_hits = 0
        observed_dart_pages = 0
        observed = False
        dart_observed = False
        diagnostics: list[dict[str, Any]] = []
        dart_diagnostics = SearchExecutionDiagnostics()
        deadline = DeadlineBudget(self.clock() + defaults.STANDARD_HARD_TIMEOUT_SECONDS, clock=self.clock)
        corp_code = resolved_company_code
        for window in windows:
            window_start, window_end = window.date_from, window.date_to
            if deadline.remaining() <= 0:
                diagnostics.append({"reason": "preview_deadline", "unmeasured_windows": len(windows)})
                break
            if opendart_available:
                for disclosure_type in type_scopes:
                    try:
                        page = self.opendart.list_page(
                            date_from=window_start,
                            date_to=window_end,
                            page_no=1,
                            corp_code=corp_code,
                            disclosure_type=disclosure_type,
                            deadline=deadline,
                        )
                        rows = page.get("list", [])
                        observed_list_rows += max(int(page.get("total_count", len(rows))), len(rows))
                        observed = True
                    except (SearchError, TypeError, AttributeError):
                        diagnostics.append({"channel": "opendart", "reason": "preview_unconfirmed"})
            if dart_available and deadline.remaining() > 0:
                try:
                    page = self.dart.search_page(
                        variants[0],
                        window_start,
                        window_end,
                        dart_diagnostics,
                        page=1,
                        mode=mode,
                        request_budget=max(defaults.STANDARD_DART_REQUEST_BUDGET, len(windows) + 2),
                        deadline=deadline,
                    )
                    window_hits = int(getattr(page, "search_count", len(page.rows)) or 0)
                    observed_dart_hits += window_hits
                    observed_dart_pages += math.ceil(window_hits / defaults.DART_EFFECTIVE_PAGE_SIZE)
                    observed = True
                    dart_observed = True
                except (SearchError, TypeError, AttributeError):
                    diagnostics.append({"channel": "dart", "reason": "preview_unconfirmed"})
        raw_estimate = max(observed_dart_hits, observed_list_rows if observed else request.target_count * 2)
        dedupe_yield = 0.65 if observed else 0.5
        estimated_unique = max(0, min(request.target_count if not request.exhaustive else raw_estimate, math.ceil(raw_estimate * dedupe_yield)))
        result_pages = observed_dart_pages if dart_observed else None
        planned_calls = estimated_list_requests + estimated_dart_requests + estimated_unique
        duration_seconds = max(1, math.ceil(planned_calls * 1.05))
        return {
            "estimated_list_requests": estimated_list_requests,
            "estimated_dart_requests": estimated_dart_requests,
            "dart_search_count": observed_dart_hits if dart_observed else None,
            "effective_dart_page_size": defaults.DART_EFFECTIVE_PAGE_SIZE,
            "estimated_dart_result_pages": result_pages,
            "planned_http_calls": planned_calls,
            "dedupe_yield_rate": dedupe_yield,
            "dedupe_yield_basis": "first_page_observation" if observed else "conservative_prior",
            "dedupe_yield_confidence": "medium" if observed else "low",
            "estimated_unique_documents": estimated_unique,
            "estimated_documents": raw_estimate,
            "request_start_interval_floor_ms": 1000,
            "dart_rate_floor_seconds": max(0, estimated_dart_requests - 1) * defaults.DART_MIN_REQUEST_INTERVAL_SECONDS,
            "estimated_duration_seconds": duration_seconds,
            "estimated_storage_bytes": estimated_unique * 4096,
            "estimation_basis": "window_first_pages" if observed else "unconfirmed_conservative_prior",
            "preview_diagnostics": diagnostics,
        }

    def _new_state(self, job_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        request = dict(plan["request"])
        request.pop("query")
        request["query"] = None
        request["normalized_query_hash"] = str(plan["scope"]["query_hash"])
        request["query_variants"] = list(plan["dart_query"]["variants"])
        request["search_mode"] = plan["dart_query"].get("mode", "contents")
        request["verification_term_groups"] = plan["dart_query"].get("verification_term_groups", [])
        type_scopes: list[str | None] = list(request.get("disclosure_types") or [None])
        windows = [
            [start.isoformat(), end.isoformat(), disclosure_type]
            for window in reversed(split_date_windows(date.fromisoformat(request["date_from"]), date.fromisoformat(request["date_to"])))
            for start, end in [(window.date_from, window.date_to)]
            for disclosure_type in type_scopes
        ]
        use_dart = (
            self.dart is not None
            and hasattr(self.dart, "search_page")
            and not request.get("disclosure_types")
        )
        dart_units = [
            [start.isoformat(), end.isoformat(), variant]
            for start, end in dart_date_windows(
                date.fromisoformat(request["date_from"]),
                date.fromisoformat(request["date_to"]),
                90,
            )
            for variant in request["query_variants"]
        ] if use_dart else []
        return {
            "schema_version": 1,
            "job_id": job_id,
            "plan_id": plan["plan_id"],
            "request": request,
            "windows": windows,
            "opendart_windows": windows,
            "phase": "dart_discovery" if use_dart else "opendart_discovery",
            "dart_units": dart_units,
            "dart_next_unit_index": 0,
            "dart_next_page": 1,
            "dart_candidates": {},
            "dart_health_checked": False,
            "next_window_index": 0,
            "next_page": 1,
            "next_row_offset": 0,
            "processed_receipts": [],
            "results": [],
            "diagnostics": [],
            "calls": 0,
            "document_concurrency": int(self.settings.get("search.document_concurrency", defaults.DOCUMENT_CONCURRENCY)),
            "document_concurrency_events": [],
            "created_at_epoch": self.wall_clock(),
            "updated_at_epoch": self.wall_clock(),
            "circuit_state": self._circuit_snapshot(),
        }

    def _execute_segment(self, state: dict[str, Any], interval_minutes: int) -> dict[str, Any]:
        hard_seconds = interval_minutes * 60
        soft_seconds = max(30, hard_seconds - 30)
        start = self.clock()
        hard_deadline = DeadlineBudget(start + hard_seconds, clock=self.clock)
        request = state["request"]
        variants = list(request["query_variants"])
        groups = [tuple(group) for group in request.get("verification_term_groups", [])]
        if self.opendart is None or not hasattr(self.opendart, "list_page"):
            return {"status": "channel_unavailable", "network_started": False, "job_id": state["job_id"]}
        processed = set(state.get("processed_receipts", []))
        windows = state["windows"]
        network_started = self._probe_restored_circuit(state, hard_deadline)
        stop_reason: str | None = None
        corp_code = request.get("resolved_company_code")
        if state.get("phase") == "dart_discovery":
            discovery_started, discovery_stop = self._discover_dart_candidates(
                state,
                hard_deadline,
                start=start,
                soft_seconds=soft_seconds,
            )
            network_started = network_started or discovery_started
            if discovery_stop is not None:
                state["circuit_state"] = self._circuit_snapshot()
                self._save_checkpoint(state)
                return {
                    "status": "continuation_confirmation_required",
                    "job_id": state["job_id"],
                    "stop_reason": discovery_stop,
                    "selected_confirmation_interval_minutes": interval_minutes,
                    "soft_deadline_seconds": soft_seconds,
                    "hard_deadline_seconds": hard_seconds,
                    "processed_document_count": len(processed),
                    "result_count": len(state["results"]),
                    "checkpoint": {
                        "phase": state["phase"],
                        "dart_next_unit_index": state.get("dart_next_unit_index"),
                        "dart_next_page": state.get("dart_next_page"),
                    },
                    "network_started": network_started,
                    "completeness_grade": "partial",
                    **self._circuit_response_fields(),
                }
            windows = state["windows"]
        try:
            while int(state["next_window_index"]) < len(windows):
                if self.clock() - start >= soft_seconds or hard_deadline.remaining() <= 0:
                    stop_reason = "confirmation_interval_ended"
                    break
                window_index = int(state["next_window_index"])
                page_no = int(state["next_page"])
                if windows[window_index][0] == "dart_candidates":
                    page = {
                        "list": list(state.get("dart_candidates", {}).values()),
                        "total_page": 1,
                        "total_count": len(state.get("dart_candidates", {})),
                    }
                else:
                    window_start = date.fromisoformat(windows[window_index][0])
                    window_end = date.fromisoformat(windows[window_index][1])
                    disclosure_type = windows[window_index][2]
                    page = self.opendart.list_page(
                        date_from=window_start,
                        date_to=window_end,
                        page_no=page_no,
                        corp_code=corp_code,
                        disclosure_type=disclosure_type,
                        deadline=hard_deadline,
                    )
                    network_started = True
                    state["calls"] = int(state.get("calls", 0)) + 1
                rows = list(page.get("list", []))
                offset = int(state.get("next_row_offset", 0))
                row_index = offset
                while row_index < len(rows):
                    if self.clock() - start >= soft_seconds or hard_deadline.remaining() <= 0:
                        state["next_row_offset"] = row_index
                        stop_reason = "confirmation_interval_ended"
                        break
                    concurrency = max(1, min(int(state.get("document_concurrency", 1)), defaults.DOCUMENT_CONCURRENCY))
                    batch: list[tuple[int, Any]] = []
                    while row_index < len(rows) and len(batch) < concurrency:
                        row = rows[row_index]
                        receipt = str(row.get("rcept_no") or "")
                        if receipt and receipt not in processed:
                            batch.append((row_index, candidate_from_list_row(row, source="opendart_batch")))
                        row_index += 1
                    if not batch:
                        state["next_row_offset"] = row_index
                        continue
                    network_started = True
                    state["calls"] = int(state.get("calls", 0)) + len(batch)
                    failed_index: int | None = None
                    failed_error: SearchError | None = None
                    outcomes: dict[int, tuple[Any, ...] | Exception] = {}
                    with ThreadPoolExecutor(max_workers=concurrency) as pool:
                        futures = {
                            index: pool.submit(self._download_evidence, candidate.receipt_no, variants, groups, hard_deadline)
                            for index, candidate in batch
                        }
                        for index, future in futures.items():
                            try:
                                outcomes[index] = future.result()
                            except Exception as exc:
                                outcomes[index] = exc
                    for index, candidate in batch:
                        outcome = outcomes[index]
                        if isinstance(outcome, SearchError):
                            adaptive = AdaptiveConcurrency(
                                defaults.DOCUMENT_CONCURRENCY,
                                current=int(state.get("document_concurrency", defaults.DOCUMENT_CONCURRENCY)),
                            )
                            if adaptive.observe(outcome):
                                state["document_concurrency"] = adaptive.current
                                state.setdefault("document_concurrency_events", []).extend(adaptive.events)
                            if failed_index is None or index < failed_index:
                                failed_index, failed_error = index, outcome
                            continue
                        if isinstance(outcome, Exception):
                            exc = SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "배치 원문 처리에 실패했습니다.")
                            if failed_index is None or index < failed_index:
                                failed_index, failed_error = index, exc
                            continue
                        evidence = outcome
                        if evidence and (
                            request.get("exhaustive")
                            or len(state["results"]) < int(request["target_count"])
                        ):
                            source_url = candidate.dart_viewer_url or dart_viewer_url(candidate.receipt_no)
                            state["results"].append(
                                {
                                    "receipt_no": candidate.receipt_no,
                                    "corp_name": candidate.corp_name,
                                    "report_name": candidate.report_name,
                                    "receipt_date": candidate.receipt_date,
                                    "viewer_url": source_url,
                                    "original_document_url": source_url,
                                    "evidence": [
                                        {**asdict(item), "csv_formula_risk": has_formula_prefix(item.text)}
                                        for item in evidence[:3]
                                    ],
                                }
                            )
                        processed.add(candidate.receipt_no)
                    state["processed_receipts"] = sorted(processed)
                    if failed_index is not None and failed_error is not None:
                        state["next_row_offset"] = failed_index
                        state["diagnostics"].append({"channel": "opendart_document", "reason": failed_error.code.value})
                        if failed_error.code == ErrorCode.OPENDART_REQUEST_LIMIT_EXCEEDED:
                            stop_reason = failed_error.code.value
                            break
                        if failed_error.code in {
                            ErrorCode.OPENDART_HTTP_RATE_LIMITED,
                            ErrorCode.OPENDART_TEMPORARY_FAILURE,
                        }:
                            stop_reason = failed_error.code.value
                            break
                        elif failed_error.code == ErrorCode.SEARCH_TIMEOUT_PARTIAL:
                            stop_reason = failed_error.code.value
                            break
                        else:
                            stop_reason = failed_error.code.value
                            break
                    else:
                        state["next_row_offset"] = row_index
                    self._save_checkpoint(state)
                    if not request.get("exhaustive") and len(state["results"]) >= int(request["target_count"]):
                        state["next_window_index"] = len(windows)
                        break
                if stop_reason:
                    break
                if int(state["next_window_index"]) >= len(windows):
                    break
                state["next_row_offset"] = 0
                total_pages = max(1, int(page.get("total_page") or math.ceil(int(page.get("total_count", 0)) / defaults.OPENDART_PAGE_COUNT) or 1))
                if page_no < total_pages:
                    state["next_page"] = page_no + 1
                else:
                    state["next_window_index"] = window_index + 1
                    state["next_page"] = 1
                self._save_checkpoint(state)
        except SearchError as exc:
            stop_reason = exc.code
            state["diagnostics"].append({"reason": exc.code, "message": str(exc)})
        state["circuit_state"] = self._circuit_snapshot()
        self._save_checkpoint(state)
        if int(state["next_window_index"]) < len(windows):
            return {
                "status": "continuation_confirmation_required",
                "job_id": state["job_id"],
                "stop_reason": stop_reason or "confirmation_interval_ended",
                "selected_confirmation_interval_minutes": interval_minutes,
                "soft_deadline_seconds": soft_seconds,
                "hard_deadline_seconds": hard_seconds,
                "processed_document_count": len(processed),
                "result_count": len(state["results"]),
                "checkpoint": {
                    "next_window_index": state["next_window_index"],
                    "next_page": state["next_page"],
                    "next_row_offset": state["next_row_offset"],
                },
                "network_started": network_started,
                "completeness_grade": "partial",
                **self._circuit_response_fields(),
            }
        record_id = self.records.new_id("search")
        for result in state["results"]:
            source_url = (
                result.get("original_document_url")
                or result.get("viewer_url")
                or dart_viewer_url(str(result["receipt_no"]))
            )
            result["viewer_url"] = source_url
            result["original_document_url"] = source_url
            result["csv_formula_risk_fields"] = [
                key
                for key in ("receipt_no", "corp_name", "report_name", "receipt_date", "viewer_url", "original_document_url")
                if has_formula_prefix(result.get(key, ""))
            ]
        record = {
            "schema_version": 1,
            "search_record_id": record_id,
            "request": request,
            "results": state["results"],
            "diagnostics": state["diagnostics"],
            "export_safety": {
                "csv_formula_prefixes": ["=", "+", "-", "@", "TAB", "CR"],
                "csv_escape": "leading_apostrophe",
                "json_preserves_original_text": True,
            },
            "created_at_epoch": self.wall_clock(),
            "retention_hours": 24,
        }
        self.records.save(record_id, record)
        self._audit_completed(state, record_id)
        self.checkpoints.delete(state["job_id"])
        return {
            "status": "completed",
            "job_id": state["job_id"],
            "search_record_id": record_id,
            "result_count": len(state["results"]),
            "processed_document_count": len(processed),
            "selected_confirmation_interval_minutes": interval_minutes,
            "soft_deadline_seconds": soft_seconds,
            "hard_deadline_seconds": hard_seconds,
            "available_export_formats": ["csv", "json"],
            "export_requires_output_directory": True,
            "retention_hours": 24,
            "network_started": network_started,
            "completeness_grade": "complete",
            **self._circuit_response_fields(),
        }

    def _save_checkpoint(self, state: dict[str, Any]) -> None:
        state["updated_at_epoch"] = self.wall_clock()
        self.checkpoints.save(str(state["job_id"]), state)

    def _download_evidence(
        self,
        receipt_no: str,
        variants: list[str],
        groups: list[tuple[str, ...]],
        deadline: DeadlineBudget,
    ) -> tuple[Any, ...]:
        document = self.opendart.download_document(receipt_no, deadline=deadline)
        if groups:
            return extract_cooccurrence_evidence(receipt_no, document, groups)
        return extract_evidence(receipt_no, document, variants)

    def _discover_dart_candidates(
        self,
        state: dict[str, Any],
        deadline: DeadlineBudget,
        *,
        start: float,
        soft_seconds: int,
    ) -> tuple[bool, str | None]:
        diagnostics = SearchExecutionDiagnostics()
        network_started = False
        try:
            if not state.get("dart_health_checked"):
                network_started = True
                if not self.dart.health_check(diagnostics, deadline=deadline):
                    state["phase"] = "opendart_discovery"
                    state["windows"] = state["opendart_windows"]
                    state["diagnostics"].append(
                        {"channel": "dart", "reason": "DART_FULLTEXT_FALLBACK", "fallback_source": "opendart"}
                    )
                    return network_started, None
                state["calls"] = int(state.get("calls", 0)) + diagnostics.health_check_requests
                state["dart_health_checked"] = True
            units = state.get("dart_units", [])
            while int(state.get("dart_next_unit_index", 0)) < len(units):
                if self.clock() - start >= soft_seconds or deadline.remaining() <= 0:
                    return network_started, "confirmation_interval_ended"
                unit_index = int(state["dart_next_unit_index"])
                start_date, end_date, variant = units[unit_index]
                page_no = int(state.get("dart_next_page", 1))
                before = (
                    diagnostics.health_check_requests
                    + diagnostics.mode_setup_requests
                    + diagnostics.dart_result_page_requests
                    + diagnostics.structure_retry_requests
                )
                result = self.dart.search_page(
                    variant,
                    date.fromisoformat(start_date),
                    date.fromisoformat(end_date),
                    diagnostics,
                    page=page_no,
                    request_budget=before + max(
                        defaults.STANDARD_DART_REQUEST_BUDGET,
                        int(deadline.remaining()) + 5,
                    ),
                    company=state["request"].get("resolved_company_code"),
                    deadline=deadline,
                    mode=state["request"].get("search_mode", "contents"),
                )
                after = (
                    diagnostics.health_check_requests
                    + diagnostics.mode_setup_requests
                    + diagnostics.dart_result_page_requests
                    + diagnostics.structure_retry_requests
                )
                network_started = True
                state["calls"] = int(state.get("calls", 0)) + max(1, after - before)
                for row in result.rows:
                    candidate = row_to_candidate(row, variant)
                    state["dart_candidates"].setdefault(
                        candidate.receipt_no,
                        {
                            "rcept_no": candidate.receipt_no,
                            "corp_code": candidate.corp_code,
                            "corp_name": candidate.corp_name,
                            "report_nm": "".join(candidate.report_name_prefixes) + candidate.report_name,
                            "rcept_dt": candidate.receipt_date,
                            "flr_nm": candidate.filer_name,
                            "rm": candidate.rm_raw,
                        },
                    )
                if result.classification == "normal_zero" or not result.estimated_pages or page_no >= result.estimated_pages:
                    state["dart_next_unit_index"] = unit_index + 1
                    state["dart_next_page"] = 1
                else:
                    state["dart_next_page"] = page_no + 1
                self._save_checkpoint(state)
            state["phase"] = "dart_verification"
            state["windows"] = [["dart_candidates", "", None]]
            state["next_window_index"] = 0
            state["next_page"] = 1
            state["next_row_offset"] = 0
            self._save_checkpoint(state)
            return network_started, None
        except SearchError as exc:
            if exc.code == ErrorCode.SEARCH_TIMEOUT_PARTIAL:
                return network_started, exc.code.value
            if exc.code in {
                ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN,
                ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED,
                ErrorCode.OPENDART_TEMPORARY_FAILURE,
                ErrorCode.OPENDART_HTTP_RATE_LIMITED,
            }:
                state["phase"] = "opendart_discovery"
                state["windows"] = state["opendart_windows"]
                state["next_window_index"] = 0
                state["next_page"] = 1
                state["next_row_offset"] = 0
                state["diagnostics"].append(
                    {
                        "channel": "dart",
                        "reason": "DART_FULLTEXT_FALLBACK",
                        "error": exc.code.value,
                        "fallback_source": "opendart",
                    }
                )
                return network_started, None
            raise

    def _validate_interval(self, value: int | None) -> dict[str, Any] | None:
        if value not in INTERVAL_OPTIONS:
            return {
                "status": "confirmation_interval_required",
                "allowed_minutes": list(INTERVAL_OPTIONS),
                "network_started": False,
            }
        return None

    def _circuit_snapshot(self) -> dict[str, Any] | None:
        breaker = getattr(self.dart, "breaker", None)
        if breaker is None or not hasattr(breaker, "snapshot"):
            return None
        return breaker.snapshot()

    def _restore_circuit(self, value: Any) -> None:
        breaker = getattr(self.dart, "breaker", None)
        if value and breaker is not None and hasattr(breaker, "restore"):
            breaker.restore(value)

    def _circuit_response_fields(self) -> dict[str, Any]:
        breaker = getattr(self.dart, "breaker", None)
        if breaker is None:
            return {"blocked_until": None, "blocked_seconds": 0}
        event = breaker.event()
        return {
            "blocked_until": event.get("blocked_until"),
            "blocked_seconds": breaker.remaining_blocked_seconds()
            if event.get("status") == ChannelStatus.CIRCUIT_OPEN.value
            else 0,
        }

    def _audit_completed(self, state: dict[str, Any], record_id: str) -> None:
        audit = getattr(self.engine, "audit", None)
        if audit is None:
            return
        request = state["request"]
        receipts = [item["receipt_no"] for item in state["results"]]
        audit.append_summary(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "search_lineage_id": state.get("plan_id"),
                "search_record_id": record_id,
                "mode": "approved_batch",
                "normalized_query_hash": request["normalized_query_hash"],
                "executed_query_variants": request["query_variants"],
                "search_period": {"date_from": request["date_from"], "date_to": request["date_to"]},
                "scope": {
                    "company": request.get("company"),
                    "disclosure_types": request.get("disclosure_types", []),
                },
                "candidate_receipts": list(
                    dict.fromkeys(
                        [*state.get("dart_candidates", {}).keys(), *state.get("processed_receipts", [])]
                    )
                ),
                "verified_receipts": receipts,
                "exclusion_reasons": [],
                "call_cache_retry_diagnostics": {
                    "calls": state.get("calls", 0),
                    "events": state.get("diagnostics", []),
                },
                "warning_codes": [],
                "completeness_grade": "complete",
            }
        )

    def _probe_restored_circuit(self, state: dict[str, Any], deadline: DeadlineBudget) -> bool:
        breaker = getattr(self.dart, "breaker", None)
        health_check = getattr(self.dart, "health_check", None)
        if breaker is None or health_check is None or breaker.before_request() != ChannelStatus.PROBING:
            return False
        diagnostics = SearchExecutionDiagnostics()
        try:
            healthy = bool(health_check(diagnostics, force=True, deadline=deadline))
            state["diagnostics"].append(
                {"channel": "dart", "reason": "checkpoint_circuit_probe", "healthy": healthy}
            )
        except SearchError as exc:
            state["diagnostics"].append(
                {"channel": "dart", "reason": "checkpoint_circuit_probe", "error": exc.code}
            )
        return True

    @staticmethod
    def _csv_bytes(results: list[dict[str, Any]]) -> bytes:
        stream = io.StringIO(newline="")
        fields = ["receipt_no", "corp_name", "report_name", "receipt_date", "viewer_url", "original_document_url", "evidence"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    **{key: escape_csv_cell(result.get(key, "")) for key in fields[:-1]},
                    "evidence": escape_csv_cell(
                        " | ".join(str(item.get("text", "")) for item in result.get("evidence", []))
                    ),
                }
            )
        return ("\ufeff" + stream.getvalue()).encode("utf-8")
