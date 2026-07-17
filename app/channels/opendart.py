"""OpenDART list, company-code and document channel."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from app.config.defaults import (
    CORPCODE_TTL_HOURS,
    COMPANY_LOOKUP_LIMIT,
    DOCUMENT_MAX_TEXT_MB,
    OPENDART_COMPANY_BATCH_SIZE,
    OPENDART_PAGE_COUNT,
    OPENDART_WINDOW_MONTHS,
    STANDARD_LIST_REQUEST_BUDGET,
)
from app.contracts import DisclosureCandidate, SearchExecutionDiagnostics
from app.errors import ErrorCode, SearchError
from app.http_client import HttpClient
from app.research.normalization import dart_viewer_url, parse_report_name, parse_rm
from app.security.archive_guard import read_safe_zip
from app.security.xml_guard import parse_xml_safely
from app.storage.atomic import atomic_write_bytes, atomic_write_json

from .opendart_status import ensure_success

BASE_URL = "https://opendart.fss.or.kr/api"


@dataclass(frozen=True, slots=True)
class DateWindow:
    date_from: date
    date_to: date

    @property
    def key(self) -> str:
        return f"{self.date_from.isoformat()}..{self.date_to.isoformat()}"


@dataclass(slots=True)
class ListCollection:
    candidates: list[DisclosureCandidate] = field(default_factory=list)
    complete: bool = True
    next_window_index: int | None = None
    next_page: int | None = None


@dataclass(frozen=True, slots=True)
class CompanyRecord:
    corp_code: str
    corp_name: str
    corp_eng_name: str
    stock_code: str | None
    modify_date: str


def _days_in_month(year: int, month: int) -> int:
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return (next_month - timedelta(days=1)).day


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return date(year, month, min(value.day, _days_in_month(year, month)))


def split_date_windows(date_from: date, date_to: date, months: int = OPENDART_WINDOW_MONTHS) -> list[DateWindow]:
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    windows: list[DateWindow] = []
    cursor = date_from
    while cursor <= date_to:
        next_start = _add_months(cursor, months)
        end = min(date_to, next_start - timedelta(days=1))
        windows.append(DateWindow(cursor, end))
        cursor = end + timedelta(days=1)
    return windows


def batch_companies(corp_codes: Iterable[str]) -> list[tuple[str, ...]]:
    unique = tuple(dict.fromkeys(code for code in corp_codes if code))
    return [unique[i : i + OPENDART_COMPANY_BATCH_SIZE] for i in range(0, len(unique), OPENDART_COMPANY_BATCH_SIZE)]


class CompanyDirectory:
    def __init__(self, records: Iterable[CompanyRecord]):
        self.records = tuple(records)
        self.by_code = {record.corp_code: record for record in self.records}
        self.by_stock = {record.stock_code: record for record in self.records if record.stock_code}
        self.by_name: dict[str, list[CompanyRecord]] = {}
        for record in self.records:
            self.by_name.setdefault(record.corp_name.casefold(), []).append(record)

    @classmethod
    def from_zip(cls, payload: bytes) -> "CompanyDirectory":
        entries = read_safe_zip(payload)
        xml = next((value for name, value in entries.items() if name.upper().endswith("CORPCODE.XML")), None)
        if xml is None:
            raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "CORPCODE.xml이 ZIP에 없습니다.")
        root = parse_xml_safely(xml)
        records = []
        for item in root.findall("list"):
            get = lambda name: (item.findtext(name) or "").strip()
            code = get("corp_code")
            name = get("corp_name")
            if code and name:
                records.append(CompanyRecord(code, name, get("corp_eng_name"), get("stock_code") or None, get("modify_date")))
        return cls(records)

    def lookup(self, query: str, limit: int = COMPANY_LOOKUP_LIMIT) -> list[CompanyRecord]:
        key = query.strip().casefold()
        if query in self.by_code:
            return [self.by_code[query]]
        if query in self.by_stock:
            return [self.by_stock[query]]
        exact = self.by_name.get(key)
        if exact:
            return exact[:limit]
        return [record for record in self.records if key in record.corp_name.casefold()][:limit]


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "euc-kr", "cp949"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def normalize_document_zip(payload: bytes) -> str:
    entries = read_safe_zip(payload)
    parts: list[str] = []
    for name in sorted(entries):
        text = _decode(entries[name])
        if "<!DOCTYPE" in text[:8192].upper() or "<!ENTITY" in text[:8192].upper():
            raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "원문 XML의 외부 엔터티를 차단했습니다.")
        try:
            root = parse_xml_safely(text)
            extracted = " ".join(value.strip() for value in root.itertext() if value.strip())
        except SearchError:
            parser = _TextExtractor()
            try:
                parser.feed(text)
                extracted = " ".join(value.strip() for value in parser.parts if value.strip())
            except Exception as exc:
                raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, f"{name} 원문을 해석하지 못했습니다.") from exc
        if extracted:
            parts.append(extracted)
    result = re.sub(r"\s+", " ", "\n".join(parts)).strip()
    if len(result.encode("utf-8")) > DOCUMENT_MAX_TEXT_MB * 1024 * 1024:
        result = result.encode("utf-8")[: DOCUMENT_MAX_TEXT_MB * 1024 * 1024].decode("utf-8", errors="ignore")
    return result


def candidate_from_list_row(row: dict, *, source: str = "opendart") -> DisclosureCandidate:
    receipt = str(row.get("rcept_no", "")).strip()
    prefixes, report_name, unknown_prefix_combination = parse_report_name(str(row.get("report_nm", "")))
    rm_raw = str(row.get("rm", "") or "")
    flags, unknown = parse_rm(rm_raw)
    market = next((flag for flag in flags if flag in {"유", "코", "넥"}), None)
    origin = "regulator_required" if any(prefix in {"[정정명령부과]", "[정정제출요구]"} for prefix in prefixes) else None
    return DisclosureCandidate(
        candidate_id=receipt,
        corp_code=str(row.get("corp_code") or "") or None,
        corp_name=str(row.get("corp_name") or "").strip(),
        stock_code=str(row.get("stock_code") or "").strip() or None,
        report_name=report_name,
        report_name_prefixes=prefixes,
        receipt_no=receipt,
        receipt_date=str(row.get("rcept_dt") or ""),
        filer_name=str(row.get("flr_nm") or "").strip(),
        rm_raw=rm_raw,
        rm_flags=flags,
        unknown_rm_flags=unknown,
        market_jurisdiction=market,
        includes_consolidated_part="연" in flags,
        amendment_origin=origin,
        source_channels=(source,),
        matched_terms=(),
        matched_sections=(),
        fulltext_match_scope="not_applicable",
        fulltext_row_tags=(),
        mechanical_score=0.0,
        original_receipt_no=None,
        amendment_chain_id=None,
        chain_complete=False,
        chain_confidence="unconfirmed",
        event_id=None,
        verification_status="unverified",
        dart_viewer_url=dart_viewer_url(receipt),
        unknown_prefix_combination=unknown_prefix_combination,
        rm_combination_confidence="unconfirmed" if "채" in flags and len(rm_raw) > 1 else "not_applicable",
    )


class OpenDartClient:
    def __init__(self, api_key: str, http: HttpClient | None = None):
        if not api_key:
            raise SearchError(ErrorCode.API_KEY_MISSING, "OpenDART API 키가 필요합니다.")
        self._api_key = api_key
        self.http = http or HttpClient()
        self.requests_started = 0

    def _request(self, method: str, url: str, **kwargs):
        self.requests_started += 1
        return self.http.request(method, url, **kwargs)

    def _json(self, endpoint: str, params: dict) -> dict:
        response = self._request("GET", f"{BASE_URL}/{endpoint}", params={"crtfc_key": self._api_key, **params})
        try:
            return response.json()
        except (ValueError, UnicodeError) as exc:
            raise SearchError(ErrorCode.OPENDART_TEMPORARY_FAILURE, "OpenDART JSON 응답을 해석하지 못했습니다.") from exc

    def list_page(self, *, date_from: date, date_to: date, page_no: int = 1, corp_code: str | None = None, corp_cls: str | None = None, disclosure_type: str | None = None) -> dict:
        params = {
            "corp_code": corp_code,
            "bgn_de": date_from.strftime("%Y%m%d"),
            "end_de": date_to.strftime("%Y%m%d"),
            "pblntf_ty": disclosure_type,
            "corp_cls": corp_cls,
            "sort": "date",
            "sort_mth": "desc",
            "page_no": page_no,
            "page_count": OPENDART_PAGE_COUNT,
        }
        payload = self._json("list.json", params)
        # Only status 900 permits one conservative application-level retry.
        # Limit/service/auth/maintenance statuses must never be retried here.
        if str(payload.get("status")) == "900":
            payload = self._json("list.json", params)
        ensure_success(payload)
        return payload

    def collect_lists(
        self,
        *,
        date_from: date,
        date_to: date,
        diagnostics: SearchExecutionDiagnostics,
        request_budget: int = STANDARD_LIST_REQUEST_BUDGET,
        corp_code: str | None = None,
        corp_cls: str | None = None,
        disclosure_type: str | None = None,
        start_window: int = 0,
        start_page: int = 1,
    ) -> ListCollection:
        windows = list(reversed(split_date_windows(date_from, date_to)))
        seen: set[str] = set()
        result = ListCollection()
        for window_index, window in enumerate(windows[start_window:], start=start_window):
            page = start_page if window_index == start_window else 1
            while True:
                if diagnostics.actual_list_requests >= request_budget:
                    result.complete = False
                    result.next_window_index = window_index
                    result.next_page = page
                    return result
                before_requests = self.requests_started
                try:
                    payload = self.list_page(date_from=window.date_from, date_to=window.date_to, page_no=page, corp_code=corp_code, corp_cls=corp_cls, disclosure_type=disclosure_type)
                finally:
                    diagnostics.actual_list_requests += self.requests_started - before_requests
                diagnostics.measured_total_count_by_window[window.key] = int(payload.get("total_count", 0))
                diagnostics.measured_total_pages_by_window[window.key] = int(payload.get("total_page", 0))
                if str(payload.get("status")) == "013":
                    break
                for row in payload.get("list", []):
                    receipt = str(row.get("rcept_no", ""))
                    if receipt and receipt not in seen:
                        seen.add(receipt)
                        result.candidates.append(candidate_from_list_row(row))
                total_page = int(payload.get("total_page") or math.ceil(int(payload.get("total_count", 0)) / OPENDART_PAGE_COUNT) or 1)
                if page >= total_page:
                    break
                page += 1
            diagnostics.processed_window_count += 1
            diagnostics.sampled_window_count += 1
        diagnostics.estimation_basis = "exact_all_windows"
        diagnostics.estimation_confidence = "high"
        return result

    def download_document(self, receipt_no: str) -> str:
        params = {"crtfc_key": self._api_key, "rcept_no": receipt_no}
        response = self._request("GET", f"{BASE_URL}/document.xml", params=params)
        if not response.body.startswith(b"PK"):
            payload = self._non_zip_payload(response.body, "원문")
            if str(payload.get("status")) == "900":
                response = self._request("GET", f"{BASE_URL}/document.xml", params=params)
                if not response.body.startswith(b"PK"):
                    payload = self._non_zip_payload(response.body, "원문")
                    ensure_success(payload, allow_no_data=False)
            else:
                ensure_success(payload, allow_no_data=False)
        return normalize_document_zip(response.body)

    def load_company_directory(self, cache_path: Path, *, now: float | None = None) -> CompanyDirectory:
        current = time.time() if now is None else now
        if cache_path.exists() and current - cache_path.stat().st_mtime < CORPCODE_TTL_HOURS * 3600:
            return CompanyDirectory.from_zip(cache_path.read_bytes())
        params = {"crtfc_key": self._api_key}
        response = self._request("GET", f"{BASE_URL}/corpCode.xml", params=params)
        if not response.body.startswith(b"PK"):
            payload = self._non_zip_payload(response.body, "회사코드")
            if str(payload.get("status")) == "900":
                response = self._request("GET", f"{BASE_URL}/corpCode.xml", params=params)
                if not response.body.startswith(b"PK"):
                    ensure_success(self._non_zip_payload(response.body, "회사코드"), allow_no_data=False)
            else:
                ensure_success(payload, allow_no_data=False)
        directory = CompanyDirectory.from_zip(response.body)
        atomic_write_bytes(cache_path, response.body)
        atomic_write_json(cache_path.with_suffix(".manifest.json"), {"fetched_at_epoch": current, "ttl_hours": CORPCODE_TTL_HOURS, "record_count": len(directory.records)})
        return directory

    @staticmethod
    def _non_zip_payload(body: bytes, label: str) -> dict:
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            try:
                root = parse_xml_safely(body)
                return {child.tag: child.text for child in root}
            except SearchError as exc:
                raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, f"{label} 응답이 ZIP 또는 오류 JSON/XML이 아닙니다.") from exc
