"""DART full-text HTML adapter using the Stage 0.6 measured contract."""

from __future__ import annotations

import html
import math
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Callable, Iterable

from app.config.defaults import (
    DART_EFFECTIVE_PAGE_SIZE,
    DART_MIN_REQUEST_INTERVAL_SECONDS,
    STANDARD_DART_REQUEST_BUDGET,
    STRUCTURE_CIRCUIT_SECONDS,
)
from app.contracts import ChannelStatus, DisclosureCandidate, SearchExecutionDiagnostics
from app.errors import ErrorCode, SearchError
from app.http_client import HttpClient
from app.research.normalization import dart_viewer_url, parse_report_name

from .health import CircuitBreaker

DART_BASE = "https://dart.fss.or.kr"
MODE_ENDPOINTS = {"contents": "detailSearchMain2.do", "report": "detailSearchMain.do"}


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


@dataclass(frozen=True, slots=True)
class DartSearchPage:
    classification: str
    search_count: int | None
    rows: tuple[DartResultRow, ...]
    zero_markers: tuple[str, ...]
    current_page: int
    estimated_pages: int | None


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
        self._chunks: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
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

    def handle_data(self, data: str) -> None:
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
        prefixes, report, _ = parse_report_name(report_raw)
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
        ))


def parse_search_html(text: str, current_page: int = 1) -> DartSearchPage:
    parser = _DartParser()
    parser.feed(text)
    if parser.search_count is None:
        matches = re.findall(r"검색건수\s*[:：]\s*([0-9,]+)", text)
        parser.search_count = int(matches[-1].replace(",", "")) if matches else None
    zero_markers = tuple(marker for marker in ("조회 결과가 없습니다.", "검색결과가 없습니다.", "조회된 결과가 없습니다.") if marker in text)
    if parser.rows:
        classification = "results"
    elif zero_markers or parser.search_count == 0:
        classification = "normal_zero"
    else:
        classification = "structure_failure_candidate"
    pages = math.ceil(parser.search_count / DART_EFFECTIVE_PAGE_SIZE) if parser.search_count is not None else None
    return DartSearchPage(classification, parser.search_count, tuple(parser.rows), zero_markers, current_page, pages)


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
    score = 0.0
    normalized = query.casefold().replace(" ", "")
    if normalized and normalized in row.snippet.casefold().replace(" ", ""):
        score += 10.0
    if row.match_scope in {"body", "mixed"}:
        score += 3.0
    if query.casefold() in row.report_name.casefold():
        score += 2.0
    return score


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

    def _paced_request(self, method: str, url: str, **kwargs):
        now = self.clock()
        if self._last_request_started is not None:
            remaining = DART_MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_started)
            if remaining > 0:
                self.sleeper(remaining)
        self._last_request_started = self.clock()
        return self.http.request(method, url, **kwargs)

    @staticmethod
    def _form(query: str, date_from: date, date_to: date, mode: str, page: int, company: str | None = None) -> dict[str, str]:
        compact_from = date_from.strftime("%Y%m%d")
        compact_to = date_to.strftime("%Y%m%d")
        form = {
            "currentPage": str(page), "maxResults": str(DART_EFFECTIVE_PAGE_SIZE), "maxLinks": "10",
            "sort": "DATE", "sortType": "desc", "option": mode,
            "keyword": query if mode == "contents" else "", "b_keyword": query if mode == "contents" else "",
            "reportName": query if mode == "report" else "", "b_reportName": query if mode == "report" else "",
            "textCrpCik": "", "textCrpNm": company or "", "flrCik": "", "textPresenterNm": "",
            "startDate": compact_from, "endDate": compact_to,
            "b_startDate": compact_from, "b_endDate": compact_to,
            "docType": "", "b_docType": "", "dspTypeTab": "", "b_dspType": "",
            "isSort": "false", "isTab": "false", "tocSrch": "", "lateKeyword": "",
            "b_textCrpCik": "", "b_flrCik": "", "b_textPresenterNm": "",
            "b_synonym": "", "b_reSearch": "", "reportNamePopYn": "N", "autoSearch": "N", "decadeType": "",
        }
        return form

    def _ensure_available(self, diagnostics: SearchExecutionDiagnostics) -> None:
        status = self.breaker.before_request()
        if status == ChannelStatus.CIRCUIT_OPEN:
            diagnostics.fallback_used = True
            event = self.breaker.event()
            diagnostics.channel_health_events.append(event)
            blocked = max(0, int((event.get("blocked_until_epoch") or 0) - time.time()))
            raise SearchError(
                ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN,
                "DART 본문검색 채널이 차단되어 OpenDART 원문검색으로 즉시 폴백합니다.",
                details={"blocked_seconds": blocked, "fallback_source": "opendart_document_search", **event},
            )

    def health_check(self, diagnostics: SearchExecutionDiagnostics) -> bool:
        self._ensure_available(diagnostics)
        try:
            response = self._paced_request("GET", f"{DART_BASE}/dsab007/main.do")
            diagnostics.health_check_requests += 1
            healthy = response.status == 200 and b"detailSearch" in response.body
        except SearchError:
            healthy = False
        if healthy:
            self.breaker.success()
        else:
            self.breaker.failure("network")
        diagnostics.channel_health_events.append(self.breaker.event())
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
    ) -> DartSearchPage:
        self._ensure_available(diagnostics)
        if mode not in MODE_ENDPOINTS:
            raise ValueError("mode must be contents or report")
        form = self._form(query, date_from, date_to, mode, page, company)
        referer = {"Referer": f"{DART_BASE}/dsab007/main.do", "X-Requested-With": "XMLHttpRequest"}
        try:
            if self._active_mode != mode:
                if diagnostics.health_check_requests + diagnostics.mode_setup_requests + diagnostics.dart_result_page_requests >= request_budget:
                    raise SearchError(ErrorCode.DOCUMENT_BUDGET_EXCEEDED, "DART 요청예산이 소진되었습니다.")
                self._paced_request("POST", f"{DART_BASE}/dsab007/{MODE_ENDPOINTS[mode]}", form=form, headers=referer)
                diagnostics.mode_setup_requests += 1
                self._active_mode = mode
            if diagnostics.health_check_requests + diagnostics.mode_setup_requests + diagnostics.dart_result_page_requests >= request_budget:
                raise SearchError(ErrorCode.DOCUMENT_BUDGET_EXCEEDED, "DART 요청예산이 소진되었습니다.")
            response = self._paced_request("POST", f"{DART_BASE}/dsab007/search.ax", form=form, headers=referer)
            diagnostics.dart_result_page_requests += 1
            parsed = parse_search_html(response.body.decode("utf-8", errors="replace"), page)
            if parsed.classification == "structure_failure_candidate":
                # One status-diagnostic replay is required before structure failure is confirmed.
                if diagnostics.health_check_requests + diagnostics.mode_setup_requests + diagnostics.dart_result_page_requests < request_budget:
                    retry = self._paced_request("POST", f"{DART_BASE}/dsab007/search.ax", form=form, headers=referer)
                    # This replay is a structure-status diagnosis, not a new result page.
                    diagnostics.health_check_requests += 1
                    parsed = parse_search_html(retry.body.decode("utf-8", errors="replace"), page)
                if parsed.classification == "structure_failure_candidate":
                    self.breaker.failure("structure_or_access")
                    self.breaker.failure("structure_or_access")
                    diagnostics.channel_health_events.append(self.breaker.event())
                    diagnostics.fallback_used = True
                    raise SearchError(
                        ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED,
                        "DART 본문검색 구조 또는 접근 방식 변경이 의심되어 15분간 차단하고 OpenDART로 폴백합니다.",
                        details={"blocked_seconds": STRUCTURE_CIRCUIT_SECONDS, "fallback_source": "opendart_document_search"},
                    )
            self.breaker.success()
            return parsed
        except SearchError as exc:
            if exc.code == ErrorCode.OPENDART_TEMPORARY_FAILURE:
                self.breaker.failure("network")
                diagnostics.channel_health_events.append(self.breaker.event())
                if self.breaker.state.status == ChannelStatus.CIRCUIT_OPEN:
                    diagnostics.fallback_used = True
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
    ) -> list[DisclosureCandidate]:
        rows: list[DartResultRow] = []
        query_by_receipt: dict[str, str] = {}
        for query in queries:
            page = 1
            while True:
                result = self.search_page(query, date_from, date_to, diagnostics, page=page, request_budget=request_budget, company=company)
                for row in result.rows:
                    rows.append(row)
                    query_by_receipt.setdefault(row.receipt_no, query)
                if max_unique and len({row.receipt_no for row in rows}) >= max_unique:
                    diagnostics.latest_first_bias = bool(result.estimated_pages and page < result.estimated_pages)
                    break
                if result.classification == "normal_zero" or not result.estimated_pages or page >= result.estimated_pages:
                    break
                if diagnostics.health_check_requests + diagnostics.mode_setup_requests + diagnostics.dart_result_page_requests >= request_budget:
                    diagnostics.latest_first_bias = True
                    break
                page += 1
            if max_unique and len({row.receipt_no for row in rows}) >= max_unique:
                break
            if diagnostics.health_check_requests + diagnostics.mode_setup_requests + diagnostics.dart_result_page_requests >= request_budget:
                break
        merged = merge_duplicate_rows(rows)
        candidates = [row_to_candidate(row, query_by_receipt[row.receipt_no]) for row in merged]
        return sorted(candidates, key=lambda item: (item.mechanical_score, item.receipt_date, item.receipt_no), reverse=True)
