"""DART full-text HTML adapter using the Stage 0.6 measured contract."""

from __future__ import annotations

import html
import math
import re
import time
import threading
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Callable, Iterable
from functools import lru_cache
from pathlib import Path

from app.config.defaults import (
    DART_EFFECTIVE_PAGE_SIZE,
    DART_FORM_MAX_RESULTS,
    DART_MAX_LINKS,
    DART_MIN_REQUEST_INTERVAL_SECONDS,
    STANDARD_DART_REQUEST_BUDGET,
    STRUCTURE_CIRCUIT_SECONDS,
)
from app.contracts import ChannelStatus, DisclosureCandidate, SearchExecutionDiagnostics
from app.errors import ErrorCode, SearchError
from app.http_client import DeadlineBudget, HttpClient
from app.research.normalization import dart_viewer_url, parse_report_name
from app.rules.validation import load_rule_file

from .health import CircuitBreaker

DART_BASE = "https://dart.fss.or.kr"
MODE_ENDPOINTS = {"contents": "detailSearchMain2.do", "report": "detailSearchMain.do"}

# HTML void elements emit no end tag, so they must not affect depth counters.
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


@dataclass(frozen=True, slots=True)
class DartResultRow:
    receipt_no: str
    corp_code: str | None
    company: str
    market: str | None
    report_name: str
    report_name_prefixes: tuple[str, ...]
    snippet: str
    disclosure_group: str | None
    match_scope: str
    filer_name: str
    receipt_date: str
    row_tags: tuple[str, ...]
    unknown_prefix_combination: bool = False


@dataclass(frozen=True, slots=True)
class DartSearchPage:
    classification: str
    search_count: int | None
    rows: tuple[DartResultRow, ...]
    zero_markers: tuple[str, ...]
    current_page: int
    estimated_pages: int | None
    linked_last_page: int | None
    pagination_contract_changed: bool


@dataclass(frozen=True, slots=True)
class DartWindowCollection:
    candidates: tuple[DisclosureCandidate, ...]
    windows: tuple[dict[str, object], ...]
    complete: bool
    continuous: bool


class _DartParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.search_count: int | None = None
        self.in_count = False
        self.in_tr = False
        self.cell_index = -1
        self.cell_depth = 0
        self.row: dict[str, object] = {}
        self.rows: list[DartResultRow] = []
        self._report_depth = 0
        self._company_depth = 0
        self._market_depth = 0
        self._result_table_depth = 0
        self.zero_markers: list[str] = []
        self._chunks: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if self._result_table_depth and tag not in _VOID_TAGS:
            self._result_table_depth += 1
        elif tag == "table" and "tbWideList" in classes:
            self._result_table_depth = 1
        if values.get("id") == "searchCnt":
            self.in_count = True
        if tag == "tr":
            self.in_tr = True
            self.cell_index = -1
            self.row = {}
            self._chunks = {"report": [], "company": [], "market": [], "snippet": [], "info": [], "date": []}
            return
        if not self.in_tr:
            return
        if tag in {"th", "td"}:
            self.cell_index += 1
            self.cell_depth += 1
        if tag == "a":
            href = values.get("href") or ""
            receipt = re.search(r"[?&]rcpNo=(20\d{12})(?:&|$)", href)
            if receipt:
                self.row["receipt_no"] = receipt.group(1)
                self._report_depth += 1
            if "company" in classes:
                self._company_depth += 1
            corp = re.search(r"openCorpInfoNew\('([^']+)'", href)
            if corp:
                self.row["corp_code"] = corp.group(1)
        if tag == "span" and any(name.startswith("tagCom_") for name in classes):
            self._market_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.in_count and tag in {"h4", "div", "span"}:
            self.in_count = False
        if not self.in_tr:
            if self._result_table_depth and tag not in _VOID_TAGS:
                self._result_table_depth -= 1
            return
        if tag == "a":
            if self._report_depth:
                self._report_depth -= 1
            if self._company_depth:
                self._company_depth -= 1
        if tag == "span" and self._market_depth:
            self._market_depth -= 1
        if tag in {"th", "td"} and self.cell_depth:
            self.cell_depth -= 1
        if tag == "tr":
            self._finish_row()
            self.in_tr = False
        if self._result_table_depth and tag not in _VOID_TAGS:
            self._result_table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._result_table_depth:
            for marker in ("조회 결과가 없습니다.",):
                if marker in data and marker not in self.zero_markers:
                    self.zero_markers.append(marker)
        if self.in_count:
            match = re.search(r"검색건수\s*[:：]\s*([0-9,]+)", data)
            if match:
                self.search_count = int(match.group(1).replace(",", ""))
        if not self.in_tr:
            return
        if self._company_depth:
            self._chunks["company"].append(data)
        if self._market_depth:
            self._chunks["market"].append(data)
        if self._report_depth:
            self._chunks["report"].append(data)
        if self.cell_index == 1:
            self._chunks["snippet"].append(data)
        elif self.cell_index == 2:
            self._chunks["info"].append(data)
        elif self.cell_index == 3:
            self._chunks["date"].append(data)

    def _finish_row(self) -> None:
        receipt = self.row.get("receipt_no")
        if not receipt:
            return
        report_raw = _clean(" ".join(self._chunks["report"]))
        prefixes, report, unknown_prefix_combination = parse_report_name(report_raw)
        info = _clean(" ".join(self._chunks["info"]))
        tags = tuple(re.findall(r"\[([^\]]+)\]", info))
        scope = "body" if "본문" in tags else "attachment" if "첨부문서" in tags else "mixed"
        group = next((tag for tag in tags if tag not in {"본문", "첨부문서"}), None)
        filer = _clean(info.split("제출인", 1)[-1].lstrip(" :：")) if "제출인" in info else ""
        self.rows.append(DartResultRow(
            receipt_no=str(receipt),
            corp_code=str(self.row.get("corp_code") or "") or None,
            company=_clean(" ".join(self._chunks["company"])),
            market=_clean(" ".join(self._chunks["market"])) or None,
            report_name=report,
            report_name_prefixes=prefixes,
            snippet=_clean(" ".join(self._chunks["snippet"])),
            disclosure_group=group,
            match_scope=scope,
            filer_name=filer,
            receipt_date=_clean(" ".join(self._chunks["date"]).replace(".", "")),
            row_tags=tags,
            unknown_prefix_combination=unknown_prefix_combination,
        ))


def parse_search_html(text: str, current_page: int = 1) -> DartSearchPage:
    parser = _DartParser()
    parser.feed(text)
    zero_markers = tuple(parser.zero_markers)
    if parser.rows:
        classification = "results"
    elif zero_markers or parser.search_count == 0:
        classification = "normal_zero"
    else:
        classification = "structure_failure_candidate"
    pages = math.ceil(parser.search_count / DART_EFFECTIVE_PAGE_SIZE) if parser.search_count is not None else None
    linked_pages = tuple(int(value) for value in re.findall(r"\bsearch\(\s*(\d+)\s*\)", text))
    linked_last_page = max((current_page, *linked_pages)) if linked_pages else None
    pagination_changed = _pagination_contract_changed(
        search_count=parser.search_count,
        row_count=len(parser.rows),
        current_page=current_page,
        linked_last_page=linked_last_page,
        classification=classification,
    )
    return DartSearchPage(
        classification, parser.search_count, tuple(parser.rows), zero_markers,
        current_page, pages, linked_last_page, pagination_changed,
    )


def _pagination_contract_changed(
    *,
    search_count: int | None,
    row_count: int,
    current_page: int,
    linked_last_page: int | None,
    classification: str,
) -> bool:
    """Compare a result page with the measured ten-row paging contract."""
    if classification == "normal_zero" or search_count is None or search_count <= 0:
        return False
    estimated_pages = math.ceil(search_count / DART_EFFECTIVE_PAGE_SIZE)
    if linked_last_page is not None and linked_last_page != estimated_pages:
        return True
    if current_page > estimated_pages:
        return False
    expected_rows = min(
        DART_EFFECTIVE_PAGE_SIZE,
        search_count - ((current_page - 1) * DART_EFFECTIVE_PAGE_SIZE),
    )
    return row_count != expected_rows


def merge_duplicate_rows(rows: Iterable[DartResultRow]) -> list[DartResultRow]:
    merged: dict[str, DartResultRow] = {}
    order: list[str] = []
    for row in rows:
        current = merged.get(row.receipt_no)
        if current is None:
            merged[row.receipt_no] = row
            order.append(row.receipt_no)
            continue
        scopes = {current.match_scope, row.match_scope}
        preferred = row if row.match_scope == "body" and current.match_scope != "body" else current
        merged[row.receipt_no] = replace(
            preferred,
            match_scope="mixed" if scopes == {"body", "attachment"} else preferred.match_scope,
            row_tags=tuple(dict.fromkeys((*current.row_tags, *row.row_tags))),
        )
    return [merged[key] for key in order]


def mechanical_score(row: DartResultRow, query: str) -> float:
    weights = _ranking_weights()
    score = 0.0
    normalized = query.casefold().replace(" ", "")
    if normalized and normalized in row.snippet.casefold().replace(" ", ""):
        score += weights["exact_compact_snippet"]
    if row.match_scope in {"body", "mixed"}:
        score += weights["body_or_mixed"]
    if query.casefold() in row.report_name.casefold():
        score += weights["report_name"]
    return score


@lru_cache(maxsize=1)
def _ranking_weights() -> dict[str, float]:
    path = Path(__file__).resolve().parents[1] / "rules" / "ranking_rules.yaml"
    payload = load_rule_file(path, "ranking")
    return {key: float(value) for key, value in payload["weights"].items()}


def row_to_candidate(row: DartResultRow, query: str) -> DisclosureCandidate:
    return DisclosureCandidate(
        candidate_id=row.receipt_no,
        corp_code=row.corp_code,
        corp_name=row.company,
        stock_code=None,
        report_name=row.report_name,
        report_name_prefixes=row.report_name_prefixes,
        receipt_no=row.receipt_no,
        receipt_date=row.receipt_date,
        filer_name=row.filer_name,
        rm_raw="",
        rm_flags=(),
        unknown_rm_flags=(),
        market_jurisdiction=row.market,
        includes_consolidated_part=False,
        amendment_origin=None,
        source_channels=("dart_fulltext",),
        matched_terms=(query,),
        matched_sections=(),
        fulltext_match_scope=row.match_scope,
        fulltext_row_tags=row.row_tags,
        mechanical_score=mechanical_score(row, query),
        original_receipt_no=None,
        amendment_chain_id=None,
        chain_complete=False,
        chain_confidence="unconfirmed",
        event_id=None,
        verification_status="unverified",
        dart_viewer_url=dart_viewer_url(row.receipt_no),
        unknown_prefix_combination=row.unknown_prefix_combination,
    )


def dart_date_windows(date_from: date, date_to: date, window_days: int) -> list[tuple[date, date]]:
    if date_from > date_to or window_days <= 0:
        raise ValueError("invalid DART date window")
    windows = []
    cursor = date_from
    while cursor <= date_to:
        end = min(date_to, cursor + timedelta(days=window_days - 1))
        windows.append((cursor, end))
        cursor = end + timedelta(days=1)
    return windows


class DartFulltextClient:
    def __init__(
        self,
        http: HttpClient | None = None,
        *,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.http = http or HttpClient()
        self.breaker = breaker or CircuitBreaker()
        self.clock = clock
        self.sleeper = sleeper
        self._last_request_started: float | None = None
        self._active_mode: str | None = None
        self._health_confirmed = False
        self._session_generation = getattr(self.http, "session_generation", 0)
        self._request_lock = threading.Lock()

    def _invalidate_session_state(self) -> None:
        self._active_mode = None
        self._health_confirmed = False

    def _sync_session_state(self) -> None:
        generation = getattr(self.http, "session_generation", self._session_generation)
        if generation != self._session_generation:
            self._session_generation = generation
            self._invalidate_session_state()

    def reset_session(self) -> None:
        """Explicitly discard the known DART mode and health-success cache."""
        recreate = getattr(self.http, "recreate_cookie_jar", None)
        if callable(recreate):
            recreate()
        self._session_generation = getattr(self.http, "session_generation", self._session_generation + 1)
        self._last_request_started = None
        self._invalidate_session_state()

    def _paced_request(self, method: str, url: str, *, deadline: DeadlineBudget | None = None, **kwargs):
        with self._request_lock:
            self._sync_session_state()
            if deadline is not None:
                deadline.require_remaining("dart_pacing")
            now = self.clock()
            if self._last_request_started is not None:
                remaining = DART_MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_started)
                if remaining > 0:
                    if deadline is not None and deadline.remaining() <= remaining:
                        deadline.backoff_blocked = True
                        raise SearchError(
                            ErrorCode.SEARCH_TIMEOUT_PARTIAL,
                            "DART 요청 간격 대기 뒤 요청을 시작할 시간이 없어 부분 결과로 종료합니다.",
                            details={"stage": "dart_pacing", "deadline_limited_timeout": deadline.deadline_limited_timeout},
                        )
                    self.sleeper(remaining)
                    if deadline is not None:
                        deadline.require_remaining("dart_pacing")
            self._last_request_started = self.clock()
            return self.http.request(method, url, deadline=deadline, **kwargs)

    @staticmethod
    def _form(query: str, date_from: date, date_to: date, mode: str, page: int, company: str | None = None) -> dict[str, str]:
        compact_from = date_from.strftime("%Y%m%d")
        compact_to = date_to.strftime("%Y%m%d")
        company_code = company if company and company.isdigit() and len(company) == 8 else ""
        company_name = "" if company_code else company or ""
        form = {
            "currentPage": str(page), "maxResults": str(DART_FORM_MAX_RESULTS), "maxLinks": str(DART_MAX_LINKS),
            "sort": "DATE", "sortType": "desc", "option": mode,
            "keyword": query if mode == "contents" else "", "b_keyword": query if mode == "contents" else "",
            "reportName": query if mode == "report" else "", "b_reportName": query if mode == "report" else "",
            "textCrpCik": company_code, "textCrpNm": company_name, "flrCik": "", "textPresenterNm": "",
            "startDate": compact_from, "endDate": compact_to,
            "b_startDate": compact_from, "b_endDate": compact_to,
            "docType": "", "b_docType": "", "dspTypeTab": "", "b_dspType": "",
            "isSort": "false", "isTab": "false", "tocSrch": "", "lateKeyword": "",
            "b_textCrpCik": company_code, "b_flrCik": "", "b_textPresenterNm": "",
            "b_synonym": "", "b_reSearch": "", "reportNamePopYn": "N", "autoSearch": "N", "decadeType": "",
        }
        return form

    def _ensure_available(self, diagnostics: SearchExecutionDiagnostics) -> ChannelStatus:
        self._sync_session_state()
        status = self.breaker.before_request()
        if status == ChannelStatus.CIRCUIT_OPEN:
            diagnostics.fallback_used = True
            event = self.breaker.event()
            diagnostics.channel_health_events.append(event)
            blocked = self.breaker.remaining_blocked_seconds()
            raise SearchError(
                ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN,
                "DART 본문검색 채널이 차단되어 OpenDART 원문검색으로 즉시 폴백합니다.",
                details={"blocked_seconds": blocked, "fallback_source": "opendart_document_search", **event},
            )
        return status

    @staticmethod
    def _transport_failure_class(error: SearchError) -> str:
        status = (error.details or {}).get("http_status")
        if isinstance(status, int) and 400 <= status < 500 and status not in {408, 425, 429}:
            return "structure_or_access"
        return "network"

    def _record_transport_failure(
        self,
        error: SearchError,
        diagnostics: SearchExecutionDiagnostics,
    ) -> dict[str, object]:
        self._health_confirmed = False
        failure_class = self._transport_failure_class(error)
        if failure_class == "structure_or_access":
            self.breaker.trip(failure_class)
        else:
            self.breaker.failure(failure_class)
        event: dict[str, object] = self.breaker.event()
        diagnostics.channel_health_events.append(event)
        return event

    def _circuit_error(self, event: dict[str, object], *, cause: SearchError | None = None) -> SearchError:
        details = {
            "blocked_seconds": self.breaker.remaining_blocked_seconds(),
            "fallback_source": "opendart_document_search",
            **event,
        }
        if cause is not None:
            details.update(cause.details or {})
            details["failure_class"] = event.get("failure_class")
        return SearchError(
            ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN,
            "DART 본문검색 채널이 일시 차단되어 OpenDART 원문검색으로 폴백합니다.",
            details=details,
        )

    @staticmethod
    def _request_count(diagnostics: SearchExecutionDiagnostics) -> int:
        return (
            diagnostics.health_check_requests
            + diagnostics.mode_setup_requests
            + diagnostics.dart_result_page_requests
            + diagnostics.structure_retry_requests
        )

    def health_check(
        self,
        diagnostics: SearchExecutionDiagnostics,
        *,
        force: bool = False,
        deadline: DeadlineBudget | None = None,
    ) -> bool:
        initial_status = self._ensure_available(diagnostics)
        if self._health_confirmed and not force:
            return True
        failure_class = "network"
        explicit_transport_failure = False
        try:
            diagnostics.health_check_requests += 1
            response = self._paced_request("GET", f"{DART_BASE}/dsab007/main.do", deadline=deadline)
            healthy = response.status == 200 and b"detailSearch" in response.body
            if response.status == 200 and not healthy:
                failure_class = "structure_or_access"
        except SearchError as exc:
            if exc.code == ErrorCode.SEARCH_TIMEOUT_PARTIAL:
                raise
            healthy = False
            failure_class = self._transport_failure_class(exc)
            explicit_transport_failure = True
        if healthy:
            # A main.do diagnostic only proves that the lightweight health
            # endpoint is reachable.  Do not erase a preceding search.ax
            # network failure until an actual search request succeeds.
            if initial_status in {ChannelStatus.HEALTHY, ChannelStatus.PROBING}:
                self.breaker.success()
            self._health_confirmed = True
            event = self.breaker.event()
            if initial_status == ChannelStatus.PROBING:
                event = {**event, "probe_result": "success"}
        else:
            self._health_confirmed = False
            if failure_class == "structure_or_access" and explicit_transport_failure:
                self.breaker.trip(failure_class)
            else:
                self.breaker.failure(failure_class)
            event = self.breaker.event()
            if initial_status == ChannelStatus.PROBING:
                event = {**event, "probe_result": "failure"}
        diagnostics.channel_health_events.append(event)
        return healthy

    def search_page(
        self,
        query: str,
        date_from: date,
        date_to: date,
        diagnostics: SearchExecutionDiagnostics,
        *,
        mode: str = "contents",
        page: int = 1,
        request_budget: int = STANDARD_DART_REQUEST_BUDGET,
        company: str | None = None,
        deadline: DeadlineBudget | None = None,
    ) -> DartSearchPage:
        if mode not in MODE_ENDPOINTS:
            raise ValueError("mode must be contents or report")
        self._sync_session_state()
        if self._active_mode is not None and self._active_mode != mode:
            self._invalidate_session_state()
        self._ensure_available(diagnostics)
        form = self._form(query, date_from, date_to, mode, page, company)
        referer = {"Referer": f"{DART_BASE}/dsab007/main.do", "X-Requested-With": "XMLHttpRequest"}
        try:
            if self._active_mode != mode:
                if self._request_count(diagnostics) >= request_budget:
                    raise SearchError(ErrorCode.DOCUMENT_BUDGET_EXCEEDED, "DART 요청예산이 소진되었습니다.")
                diagnostics.mode_setup_requests += 1
                self._paced_request("POST", f"{DART_BASE}/dsab007/{MODE_ENDPOINTS[mode]}", form=form, headers=referer, deadline=deadline)
                self._active_mode = mode
            if self._request_count(diagnostics) >= request_budget:
                raise SearchError(ErrorCode.DOCUMENT_BUDGET_EXCEEDED, "DART 요청예산이 소진되었습니다.")
            diagnostics.dart_result_page_requests += 1
            response = self._paced_request("POST", f"{DART_BASE}/dsab007/search.ax", form=form, headers=referer, deadline=deadline)
            parsed = parse_search_html(response.body.decode("utf-8", errors="replace"), page)
            if parsed.classification == "structure_failure_candidate":
                # The first abnormal response is only a candidate. Diagnose main.do,
                # then replay the exact search once before confirming structure failure.
                self.health_check(diagnostics, force=True, deadline=deadline)
                if self.breaker.state.status == ChannelStatus.CIRCUIT_OPEN:
                    diagnostics.fallback_used = True
                    raise self._circuit_error(self.breaker.event())
                diagnostics.structure_retry_requests += 1
                retry = self._paced_request("POST", f"{DART_BASE}/dsab007/search.ax", form=form, headers=referer, deadline=deadline)
                parsed = parse_search_html(retry.body.decode("utf-8", errors="replace"), page)
                if parsed.classification == "structure_failure_candidate":
                    self._health_confirmed = False
                    self.breaker.trip("structure_or_access")
                    diagnostics.channel_health_events.append(self.breaker.event())
                    diagnostics.fallback_used = True
                    raise SearchError(
                        ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED,
                        "DART 본문검색 구조 또는 접근 방식 변경이 의심되어 15분간 차단하고 OpenDART로 폴백합니다.",
                        details={
                            "blocked_seconds": STRUCTURE_CIRCUIT_SECONDS,
                            "fallback_source": "opendart_document_search",
                            **self.breaker.event(),
                        },
                    )
            if parsed.pagination_contract_changed:
                diagnostics.pagination_contract_changed = True
                diagnostics.pagination_contract_observations.append({
                    "query": query,
                    "current_page": page,
                    "search_count": parsed.search_count,
                    "observed_rows": len(parsed.rows),
                    "expected_page_size": DART_EFFECTIVE_PAGE_SIZE,
                    "estimated_pages": parsed.estimated_pages,
                    "linked_last_page": parsed.linked_last_page,
                })
            self.breaker.success()
            return parsed
        except SearchError as exc:
            if exc.code == ErrorCode.SEARCH_TIMEOUT_PARTIAL:
                raise
            if exc.code in {ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED, ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN}:
                raise
            if exc.code in {ErrorCode.OPENDART_TEMPORARY_FAILURE, ErrorCode.OPENDART_HTTP_RATE_LIMITED}:
                event = self._record_transport_failure(exc, diagnostics)
                diagnostics.fallback_used = True
                if self.breaker.state.status == ChannelStatus.CIRCUIT_OPEN:
                    raise self._circuit_error(event, cause=exc) from exc
                raise SearchError(
                    ErrorCode.OPENDART_TEMPORARY_FAILURE,
                    "DART 본문검색 요청이 일시 실패하여 OpenDART 원문검색으로 폴백합니다.",
                    retryable=True,
                    details={
                        "failure_class": event.get("failure_class"),
                        "fallback_source": "opendart_document_search",
                        **(exc.details or {}),
                    },
                ) from exc
            raise

    def search_variants(
        self,
        queries: Iterable[str],
        date_from: date,
        date_to: date,
        diagnostics: SearchExecutionDiagnostics,
        *,
        request_budget: int = STANDARD_DART_REQUEST_BUDGET,
        max_unique: int | None = None,
        company: str | None = None,
        deadline: DeadlineBudget | None = None,
    ) -> list[DisclosureCandidate]:
        rows: list[DartResultRow] = []
        query_by_receipt: dict[str, str] = {}
        for query in queries:
            page = 1
            while True:
                result = self.search_page(
                    query, date_from, date_to, diagnostics, page=page,
                    request_budget=request_budget, company=company, deadline=deadline,
                )
                if page == 1:
                    diagnostics.dart_linked_last_page_by_query[query] = result.linked_last_page
                for row in result.rows:
                    rows.append(row)
                    query_by_receipt.setdefault(row.receipt_no, query)
                if max_unique and len({row.receipt_no for row in rows}) >= max_unique:
                    diagnostics.latest_first_bias = bool(result.estimated_pages and page < result.estimated_pages)
                    break
                if page == 1 and result.estimated_pages:
                    remaining_requests = max(0, request_budget - self._request_count(diagnostics))
                    fully_pageable = result.estimated_pages - page <= remaining_requests
                    diagnostics.fully_pageable_by_query[query] = fully_pageable
                    if not fully_pageable:
                        diagnostics.latest_first_bias = True
                        break
                if result.classification == "normal_zero" or not result.estimated_pages or page >= result.estimated_pages:
                    break
                if self._request_count(diagnostics) >= request_budget:
                    diagnostics.latest_first_bias = True
                    break
                page += 1
            if max_unique and len({row.receipt_no for row in rows}) >= max_unique:
                break
            if self._request_count(diagnostics) >= request_budget:
                break
        merged = merge_duplicate_rows(rows)
        candidates = [row_to_candidate(row, query_by_receipt[row.receipt_no]) for row in merged]
        return sorted(candidates, key=lambda item: (item.mechanical_score, item.receipt_date, item.receipt_no), reverse=True)

    def search_date_windows(
        self,
        queries: Iterable[str],
        date_from: date,
        date_to: date,
        diagnostics: SearchExecutionDiagnostics,
        *,
        window_days: int,
        request_budget: int = STANDARD_DART_REQUEST_BUDGET,
        deadline: DeadlineBudget | None = None,
    ) -> DartWindowCollection:
        """Search contiguous inclusive windows and union by receipt number.

        This is an explicit exhaustive/batch primitive. The Stage 1 interactive
        engine does not start it automatically.
        """
        windows = dart_date_windows(date_from, date_to, window_days)
        by_receipt: dict[str, DisclosureCandidate] = {}
        coverage: list[dict[str, object]] = []
        complete = True
        for start, end in windows:
            before = self._request_count(diagnostics)
            found = self.search_variants(
                queries, start, end, diagnostics,
                request_budget=request_budget, deadline=deadline,
            )
            for candidate in found:
                previous = by_receipt.get(candidate.receipt_no)
                if previous is None or candidate.mechanical_score > previous.mechanical_score:
                    by_receipt[candidate.receipt_no] = candidate
            after = self._request_count(diagnostics)
            window_complete = after < request_budget and not diagnostics.latest_first_bias
            coverage.append({
                "date_from": start.isoformat(), "date_to": end.isoformat(),
                "request_count": after - before, "unique_receipts": len({item.receipt_no for item in found}),
                "complete": window_complete,
            })
            if not window_complete:
                complete = False
                break
        continuous = bool(windows) and windows[0][0] == date_from and windows[-1][1] == date_to and all(
            left[1] + timedelta(days=1) == right[0] for left, right in zip(windows, windows[1:])
        )
        return DartWindowCollection(tuple(by_receipt.values()), tuple(coverage), complete and len(coverage) == len(windows), continuous)
