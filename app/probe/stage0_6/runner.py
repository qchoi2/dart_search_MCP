from __future__ import annotations

import hashlib
import html
import io
import json
import math
import os
import re
import signal
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from app.probe.common import RecordedHttpClient, utc_now
from app.probe.dart_web import DART_BASE, _base_form, parse_search_html


API_BASE = "https://opendart.fss.or.kr/api"
KNOWN_RM_FLAGS = ("유", "코", "채", "넥", "공", "연", "정", "철")
QUERY_SWITCH_TERMS = ("상계납입", "주금납입채무와 상계", "출자전환")
PAGE_SIZE_VALUES = (15, 30, 50, 100)
DATE_QUERY = "상계납입"
DATE_FULL = ("20260401", "20260716")
DATE_WINDOWS = (
    ("before_boundary", "20260401", "20260531"),
    ("boundary_day", "20260601", "20260601"),
    ("after_boundary", "20260602", "20260716"),
)


WITHDRAWAL_SPECS = (
    {
        "source": "20250814000105", "follow": "20250814002198",
        "source_dt": "20250814", "follow_dt": "20250814",
        "corp_code": "00564030", "corp_name": "한국투자밸류자산운용",
        "flr_nm": "한국투자밸류자산운용", "group": "existing_pair",
    },
    {
        "source": "20250814000119", "follow": "20250814002210",
        "source_dt": "20250814", "follow_dt": "20250814",
        "corp_code": "00564030", "corp_name": "한국투자밸류자산운용",
        "flr_nm": "한국투자밸류자산운용", "group": "existing_pair",
    },
    {
        "source": "20260618000391", "follow": "20260618000384",
        "source_dt": "20260618", "follow_dt": "20260618",
        "corp_code": "01359815", "corp_name": "한울반도체",
        "flr_nm": "한울소재과학", "group": "new_pair_01",
    },
    {
        "source": "20260602000451", "follow": "20260617000211",
        "source_dt": "20260602", "follow_dt": "20260617",
        "corp_code": "00163761", "corp_name": "한창제지",
        "flr_nm": "KIM JOONYOUNG", "group": "new_pair_02",
    },
    {
        "source": "20260601001369", "follow": "20260617000145",
        "source_dt": "20260601", "follow_dt": "20260617",
        "corp_code": "00958451", "corp_name": "한선엔지니어링",
        "flr_nm": "한국선재", "group": "new_pair_03",
    },
    {
        "source": "20260521000417", "follow": "20260612000481",
        "source_dt": "20260521", "follow_dt": "20260612",
        "corp_code": "00653194", "corp_name": "앱토크롬",
        "flr_nm": "포르테 신기술조합 제240호", "group": "new_pair_04",
    },
    {
        "source": "20260508000900", "follow": "20260605000621",
        "source_dt": "20260508", "follow_dt": "20260605",
        "corp_code": "01311408", "corp_name": "에코프로머티",
        "flr_nm": "김수연", "group": "new_pair_05",
    },
    {
        "source": "20260506000665", "follow": "20260629000438",
        "source_dt": "20260506", "follow_dt": "20260629",
        "corp_code": "00146427", "corp_name": "한주에이알티",
        "flr_nm": "알에프텍", "group": "new_pair_06",
    },
    {
        "source": "20260430001757", "follow": "20260626000752",
        "source_dt": "20260430", "follow_dt": "20260626",
        "corp_code": "00230814", "corp_name": "휴림에이텍",
        "flr_nm": "휴림로봇", "group": "new_pair_07",
    },
    {
        "source": "20260430001375", "follow": "20260615000241",
        "source_dt": "20260430", "follow_dt": "20260615",
        "corp_code": "00365989", "corp_name": "모나용평",
        "flr_nm": "에이치제이디오션리조트", "group": "new_pair_08",
    },
)


BOND_WINDOWS = (
    ("20260701", "20260717"), ("20260401", "20260630"),
    ("20260101", "20260331"), ("20251001", "20251231"),
    ("20250701", "20250930"), ("20250401", "20250630"),
    ("20250101", "20250331"), ("20241001", "20241231"),
)


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("stage0_6_%Y%m%dT%H%M%SZ")


def parse_rm_flags(raw: str) -> dict[str, Any]:
    flags: list[str] = []
    unknown: list[str] = []
    for char in raw or "":
        (flags if char in KNOWN_RM_FLAGS else unknown).append(char)
    return {"raw": raw or "", "rm_flags": flags, "unknown_rm_flags": unknown}


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", value).strip("_")[:100]


def _json_object(body: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label}: response was not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}: response was not a JSON object")
    return value


def _strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _zip_text(body: bytes) -> str:
    if not body.startswith(b"PK"):
        return ""
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.file_size > 30_000_000:
                continue
            raw = archive.read(info)
            for encoding in ("utf-8", "euc-kr", "cp949"):
                try:
                    parts.append(raw.decode(encoding))
                    break
                except UnicodeDecodeError:
                    pass
    return _strip_tags("\n".join(parts))


def _snippet(text: str, needles: Iterable[str], radius: int = 260) -> str:
    compact = re.sub(r"\s+", " ", text)
    for needle in needles:
        match = re.search(re.escape(needle), compact, re.IGNORECASE)
        if match:
            return compact[max(0, match.start() - radius) : match.end() + radius]
    return compact[: min(len(compact), radius * 2)]


def _date_forms(value: str) -> tuple[str, ...]:
    year, month, day = value[:4], str(int(value[4:6])), str(int(value[6:8]))
    return (
        value, f"{year}-{int(month):02d}-{int(day):02d}",
        f"{year}.{int(month):02d}.{int(day):02d}",
        f"{year}년 {int(month):02d}월 {int(day):02d}일",
        f"{year}년{int(month):02d}월{int(day):02d}일",
        f"{year}년 {month}월 {day}일", f"{year}년{month}월{day}일",
    )


def _row_receipts(parsed: dict[str, Any]) -> list[str]:
    return [row["rcept_no"] for row in parsed.get("rows", []) if row.get("rcept_no")]


def _row_dates(parsed: dict[str, Any]) -> list[str]:
    dates: list[str] = []
    for row in parsed.get("rows", []):
        matches = re.findall(
            r"(?<!\d)(20\d{2})[.]([01]\d)[.]([0-3]\d)(?!\d)", row.get("text", "")
        )
        dates.append("".join(matches[-1]) if matches else "")
    return dates


class _FormInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form_stack: list[str] = []
        self.inputs: list[dict[str, str]] = []
        self.selects: dict[str, dict[str, Any]] = {}
        self.current_select: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {key: value or "" for key, value in attrs}
        if tag == "form":
            self.form_stack.append(data.get("id") or data.get("name") or "")
        elif tag == "input" and data.get("name"):
            self.inputs.append({
                "name": data["name"], "value": data.get("value", ""),
                "type": data.get("type", ""),
                "form": self.form_stack[-1] if self.form_stack else "",
            })
        elif tag == "select" and data.get("name"):
            name = data["name"]
            self.current_select = name
            self.selects[name] = {
                "form": self.form_stack[-1] if self.form_stack else "",
                "values": [], "selected": None,
            }
        elif tag == "option" and self.current_select:
            value = data.get("value", "")
            self.selects[self.current_select]["values"].append(value)
            if "selected" in data:
                self.selects[self.current_select]["selected"] = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.form_stack:
            self.form_stack.pop()
        elif tag == "select":
            self.current_select = None


def inspect_search_form(text: str) -> dict[str, Any]:
    parser = _FormInspector()
    parser.feed(text)
    names = {
        "currentPage", "maxResults", "maxLinks", "startDate", "endDate",
        "b_startDate", "b_endDate",
    }
    return {
        "relevant_inputs": {row["name"]: row for row in parser.inputs if row["name"] in names},
        "maxResultsCb": parser.selects.get("maxResultsCb"),
        "js_copies_maxResultsCb_to_maxResults": bool(
            re.search(r"maxResults\.value\s*=\s*maxResultsCb\.value", text)
        ),
    }


@dataclass(frozen=True)
class Stage06Paths:
    repo_root: Path
    fixture_root: Path
    run_id: str

    @property
    def raw_root(self) -> Path:
        return self.fixture_root / "raw" / self.run_id

    @property
    def golden_root(self) -> Path:
        return self.fixture_root / "golden"

    @property
    def lock_path(self) -> Path:
        return self.fixture_root / ".stage0_6.lock"

    @property
    def root_manifest(self) -> Path:
        return self.repo_root / "stage0_6_manifest.json"


class Stage06Probe:
    def __init__(
        self, *, repo_root: Path, api_key: str, api_key_source: str | None,
        run_id: str, max_requests: int, deadline_seconds: int, min_interval: float,
    ) -> None:
        fixture_root = repo_root / "tests" / "fixtures" / "probe" / "stage0_6"
        self.paths = Stage06Paths(repo_root, fixture_root, run_id)
        self.paths.raw_root.mkdir(parents=True, exist_ok=False)
        self.paths.golden_root.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key
        self.started_monotonic = time.monotonic()
        self.max_requests = max_requests
        self.deadline_seconds = deadline_seconds
        self.cancel_path = self.paths.raw_root / "CANCEL"
        self.http = RecordedHttpClient(
            self.paths.raw_root, min_interval=min_interval, max_requests=max_requests,
            deadline_monotonic=self.started_monotonic + deadline_seconds,
            cancel_path=self.cancel_path,
        )
        self._lock_fd: int | None = None
        self.findings: dict[str, Any] = {}
        self.manifest: dict[str, Any] = {
            "schema_version": 1, "stage": "0.6", "plan_version": "v18",
            "run_id": run_id, "status": "running", "started_at": utc_now(),
            "pid": os.getpid(), "concurrency": 1,
            "configured_min_interval_seconds": min_interval,
            "request_limit": max_requests, "deadline_seconds": deadline_seconds,
            "deadline_at": (
                datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)
            ).isoformat(timespec="seconds"),
            "api_key": "***MASKED***", "api_key_source": api_key_source,
            "cookies_recorded": False, "tls_certificate_verification": True,
            "raw_root": str(self.paths.raw_root.relative_to(repo_root)).replace("\\", "/"),
            "golden_root": str(self.paths.golden_root.relative_to(repo_root)).replace("\\", "/"),
            "request_count": 0, "stop_reason": None,
            "main_development_started": False, "stage_1_started": False,
        }

    def __enter__(self) -> "Stage06Probe":
        try:
            self._lock_fd = os.open(self.paths.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(f"probe lock already exists: {self.paths.lock_path}") from exc
        os.write(self._lock_fd, f"{os.getpid()} {self.paths.run_id}\n".encode("ascii"))
        signal.signal(signal.SIGINT, self._signal_stop)
        signal.signal(signal.SIGTERM, self._signal_stop)
        self._sync_manifests()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.manifest["status"] = "completed" if exc is None else "stopped"
        self.manifest["stop_reason"] = "completed_stage0_6_only" if exc is None else str(exc)
        self.manifest["finished_at"] = utc_now()
        self.manifest["elapsed_seconds"] = round(time.monotonic() - self.started_monotonic, 3)
        self.manifest["request_count"] = len(self.http.records)
        self.manifest["request_limit_remaining"] = self.max_requests - len(self.http.records)
        self.manifest["main_development_started"] = False
        self.manifest["stage_1_started"] = False
        self.manifest["child_processes_started"] = 0
        self._sync_manifests()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self.paths.lock_path.unlink(missing_ok=True)

    def _signal_stop(self, signum: int, _frame: Any) -> None:
        self.cancel_path.write_text(f"signal={signum}\n", encoding="ascii")

    def _sync_manifests(self) -> None:
        self.manifest["request_count"] = len(self.http.records)
        _atomic_json(self.paths.raw_root / "manifest.json", self.manifest)
        _atomic_json(self.paths.root_manifest, self.manifest)

    def _checkpoint(self) -> None:
        self._sync_manifests()

    def api_list(self, params: dict[str, Any], fixture: str) -> dict[str, Any]:
        response = self.http.request(
            "GET", f"{API_BASE}/list.json",
            params={
                "crtfc_key": self.api_key, "sort": "date", "sort_mth": "desc",
                "page_no": 1, "page_count": 100, **params,
            },
            fixture=fixture, timeout=90,
        )
        self._checkpoint()
        payload = _json_object(response.body, fixture)
        if payload.get("status") not in {"000", "013"}:
            raise RuntimeError(
                f"{fixture}: OpenDART status={payload.get('status')} message={payload.get('message')}"
            )
        return payload

    def api_document(self, rcept_no: str, fixture: str) -> str:
        response = self.http.request(
            "GET", f"{API_BASE}/document.xml",
            params={"crtfc_key": self.api_key, "rcept_no": rcept_no},
            fixture=fixture, timeout=120,
        )
        self._checkpoint()
        return _zip_text(response.body)

    def _dart_form(
        self, query: str, *, start_date: str = "20250716",
        end_date: str = "20260716", max_results: int = 15,
        include_cb: bool = False,
    ) -> dict[str, Any]:
        form = _base_form(start_date, end_date, max_results=max_results)
        form.update({
            "currentPage": "1", "option": "contents",
            "keyword": query, "b_keyword": query,
        })
        if include_cb:
            form["maxResultsCb"] = str(max_results)
        return form

    def dart_mode(self, form: dict[str, Any], fixture: str) -> dict[str, Any]:
        response = self.http.request(
            "POST", f"{DART_BASE}/dsab007/detailSearchMain2.do",
            form=form, headers={"Referer": f"{DART_BASE}/dsab007/main.do"},
            fixture=fixture, timeout=90,
        )
        self._checkpoint()
        inspected = inspect_search_form(response.text)
        inspected.update({"http_status": response.status, "fixture": response.fixture})
        return inspected

    def dart_result(
        self, form: dict[str, Any], fixture: str, *, page: int = 1,
    ) -> dict[str, Any]:
        sent = dict(form)
        sent["currentPage"] = str(page)
        response = self.http.request(
            "POST", f"{DART_BASE}/dsab007/search.ax",
            form=sent, headers={"Referer": f"{DART_BASE}/dsab007/main.do"},
            fixture=fixture, timeout=90,
        )
        self._checkpoint()
        parsed = parse_search_html(response.text)
        parsed.update({
            "http_status": response.status, "fixture": response.fixture, "page": page,
            "receipt_rows": _row_receipts(parsed), "row_dates": _row_dates(parsed),
        })
        return parsed

    def run(self, *, web_only: bool = False) -> dict[str, Any]:
        rm_not_run = {
            "status": "unconfirmed",
            "not_run_reason": "web_only_execution_without_opendart_api_key",
        }
        self.findings = {
            "schema_version": 1,
            "scope": "DEVELOPMENT_PLAN v18 stage 0.6 measurement only",
            "run_id": self.paths.run_id, "measured_at": utc_now(),
            "query_switch": self.probe_query_switch(),
            "page_size": self.probe_page_size(),
            "date_window": self.probe_date_window(),
            "rm_withdrawal": rm_not_run if web_only else self.probe_rm_withdrawal(),
            "rm_bond": rm_not_run if web_only else self.probe_rm_bond(),
        }
        self.findings["gate_summary"] = {
            "GATE-DART-QUERY-SWITCH": self.findings["query_switch"]["status"],
            "GATE-DART-PAGESIZE": self.findings["page_size"]["status"],
            "GATE-DART-DATE-WINDOW": self.findings["date_window"]["status"],
            "GATE-RM-WITHDRAWAL": self.findings["rm_withdrawal"]["status"],
            "GATE-RM-BOND": self.findings["rm_bond"]["status"],
        }
        self.findings["request_count"] = len(self.http.records)
        self.findings["request_limit"] = self.max_requests
        self.findings["main_development_started"] = False
        self.findings["stage_1_started"] = False
        self._write_golden()
        return self.findings

    def probe_query_switch(self) -> dict[str, Any]:
        transitions: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        sequence = (*QUERY_SWITCH_TERMS, QUERY_SWITCH_TERMS[0])
        for index, query in enumerate(sequence, start=1):
            form = self._dart_form(query)
            if index == 1:
                self.dart_mode(form, f"dart/query_switch/{index:02d}_{_safe(query)}_mode.html")
                previous = self.dart_result(
                    form, f"dart/query_switch/{index:02d}_{_safe(query)}_control.html"
                )
                continue
            direct = self.dart_result(
                form, f"dart/query_switch/{index:02d}_{_safe(query)}_direct.html"
            )
            self.dart_mode(form, f"dart/query_switch/{index:02d}_{_safe(query)}_mode.html")
            control = self.dart_result(
                form, f"dart/query_switch/{index:02d}_{_safe(query)}_control.html"
            )
            direct_receipts = direct["receipt_rows"]
            control_receipts = control["receipt_rows"]
            same_as_control = (
                direct["classification"] == control["classification"]
                and direct["result_count"] == control["result_count"]
                and direct_receipts == control_receipts
            )
            previous_receipts = previous["receipt_rows"] if previous else []
            stale_previous = direct_receipts == previous_receipts and direct_receipts != control_receipts
            transitions.append({
                "from_query": sequence[index - 2], "to_query": query,
                "direct_without_mode_post": {
                    "classification": direct["classification"],
                    "search_count": direct["result_count"],
                    "receipt_rows": direct_receipts, "fixture": direct["fixture"],
                },
                "control_with_mode_post": {
                    "classification": control["classification"],
                    "search_count": control["result_count"],
                    "receipt_rows": control_receipts, "fixture": control["fixture"],
                },
                "exact_match": same_as_control,
                "stale_previous_state_detected": stale_previous,
            })
            previous = control
        passed = len(transitions) == 3 and all(
            item["exact_match"] and not item["stale_previous_state_detected"]
            for item in transitions
        )
        return {
            "same_cookie_session": True, "transition_count": len(transitions),
            "transitions": transitions,
            "prior_conservative_assumption": "검색어가 바뀌면 모드설정 POST 재실행",
            "status": "passed" if passed else "failed",
        }

    def probe_page_size(self) -> dict[str, Any]:
        response = self.http.request(
            "GET", f"{DART_BASE}/dsab007/main.do",
            fixture="dart/page_size/search_main.html", timeout=90,
        )
        self._checkpoint()
        contract = inspect_search_form(response.text)
        cases: list[dict[str, Any]] = []
        for value in PAGE_SIZE_VALUES:
            form = self._dart_form("출자전환", max_results=value)
            mode = self.dart_mode(form, f"dart/page_size/max_{value:03d}_mode.html")
            result = self.dart_result(form, f"dart/page_size/max_{value:03d}.html")
            cases.append({
                "maxResults": value, "maxResultsCb_sent": False,
                "mode_response_maxResults": mode.get("relevant_inputs", {}).get("maxResults", {}).get("value"),
                "search_count": result["result_count"],
                "actual_receipt_row_count": len(result["receipt_rows"]),
                "unique_receipt_count": len(set(result["receipt_rows"])),
                "classification": result["classification"],
                "mode_fixture": mode["fixture"], "result_fixture": result["fixture"],
            })
        if max(item["actual_receipt_row_count"] for item in cases) <= 10:
            form = self._dart_form("출자전환", max_results=100, include_cb=True)
            mode = self.dart_mode(form, "dart/page_size/max_100_with_cb_mode.html")
            result = self.dart_result(form, "dart/page_size/max_100_with_cb.html")
            cases.append({
                "maxResults": 100, "maxResultsCb_sent": True,
                "mode_response_maxResults": mode.get("relevant_inputs", {}).get("maxResults", {}).get("value"),
                "search_count": result["result_count"],
                "actual_receipt_row_count": len(result["receipt_rows"]),
                "unique_receipt_count": len(set(result["receipt_rows"])),
                "classification": result["classification"],
                "mode_fixture": mode["fixture"], "result_fixture": result["fixture"],
            })
        successful = [item for item in cases if item["classification"] == "results"]
        effective = max((item["actual_receipt_row_count"] for item in successful), default=0)
        errors = [item for item in cases if item["classification"] == "structure_failure_candidate"]
        if effective > 10:
            status = "passed" if not errors else "partially_passed"
        elif successful:
            status = "failed"
        else:
            status = "unconfirmed"
        return {
            "actual_form_contract": contract,
            "allowed_dropdown_values": (contract.get("maxResultsCb") or {}).get("values", []),
            "cases": cases, "screen_search_count_field": "검색건수",
            "actual_row_metric": "search.ax의 접수번호가 있는 결과행 수",
            "effective_page_size": effective or 10,
            "prior_conservative_effective_page_size": 10, "status": status,
        }

    def _complete_date_search(self, label: str, start_date: str, end_date: str) -> dict[str, Any]:
        form = self._dart_form(DATE_QUERY, start_date=start_date, end_date=end_date)
        self.dart_mode(form, f"dart/date_window/{label}_mode.html")
        first = self.dart_result(form, f"dart/date_window/{label}_page_001.html")
        pages = [first]
        effective = max(1, len(first["receipt_rows"]))
        expected_pages = (
            math.ceil((first["result_count"] or 0) / effective)
            if first["result_count"] else 1
        )
        for page in range(2, min(expected_pages, 5) + 1):
            pages.append(self.dart_result(
                form, f"dart/date_window/{label}_page_{page:03d}.html", page=page
            ))
        receipts = [receipt for page in pages for receipt in page["receipt_rows"]]
        dates = [value for page in pages for value in page["row_dates"]]
        return {
            "label": label, "start_date": start_date, "end_date": end_date,
            "search_count": first["result_count"], "receipt_row_count": len(receipts),
            "unique_receipt_count": len(set(receipts)), "receipts": receipts,
            "row_dates": dates, "expected_pages": expected_pages,
            "pages_fetched": len(pages), "complete": expected_pages <= len(pages),
            "fixtures": [page["fixture"] for page in pages],
        }

    def probe_date_window(self) -> dict[str, Any]:
        full = self._complete_date_search("full", *DATE_FULL)
        windows = [
            self._complete_date_search(label, start, end)
            for label, start, end in DATE_WINDOWS
        ]
        full_set = set(full["receipts"])
        window_sets = [set(item["receipts"]) for item in windows]
        union = set().union(*window_sets)
        pairwise = [
            window_sets[left] & window_sets[right]
            for left in range(len(window_sets))
            for right in range(left + 1, len(window_sets))
        ]
        overlaps = sorted(set().union(*pairwise)) if pairwise else []
        boundary = next(item for item in windows if item["label"] == "boundary_day")
        boundary_dates_ok = bool(boundary["receipts"]) and all(
            value == "20260601" for value in boundary["row_dates"] if value
        )
        additive = sum((item["search_count"] or 0) for item in windows) == (
            full["search_count"] or 0
        )
        passed = (
            full["complete"] and all(item["complete"] for item in windows)
            and union == full_set and not overlaps and additive and boundary_dates_ok
        )
        partial = full["complete"] and union.issubset(full_set) and not overlaps
        return {
            "query": DATE_QUERY, "full_period": full,
            "non_overlapping_windows": windows,
            "window_union_unique_count": len(union), "full_unique_count": len(full_set),
            "missing_from_windows": sorted(full_set - union),
            "extra_in_windows": sorted(union - full_set),
            "cross_window_duplicate_receipts": overlaps,
            "search_count_additive": additive, "boundary_day": "20260601",
            "boundary_day_inclusive": boundary_dates_ok,
            "prior_conservative_assumption": "DART 날짜창 분할 비활성",
            "status": "passed" if passed else ("partially_passed" if partial else "failed"),
        }

    def _existing_withdrawal_scan(self) -> dict[str, Any]:
        root = (
            self.paths.repo_root / "tests" / "fixtures" / "probe"
            / "opendart" / "withdrawal_scan"
        )
        flagged: dict[str, dict[str, Any]] = {}
        files = list(root.glob("*.json"))
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for row in payload.get("list") or []:
                if "철" in (row.get("rm") or ""):
                    flagged[row["rcept_no"]] = row
        return {
            "fixture_root": str(root.relative_to(self.paths.repo_root)).replace("\\", "/"),
            "fixture_file_count": len(files),
            "rm_withdrawal_rows": list(flagged.values()),
            "rm_withdrawal_unique_count": len(flagged),
        }

    def _explicit_withdrawal_link(
        self, spec: dict[str, str], source: dict[str, Any],
        follow: dict[str, Any], document_text: str,
        source_document_text: str = "",
    ) -> dict[str, Any]:
        compact = re.sub(r"\s+", " ", document_text)
        receipt_reference = spec["source"] in compact
        date_reference = any(value in compact for value in _date_forms(spec["source_dt"]))
        explicit_labels = (
            "거래계획 보고일", "거래계획보고일", "거래계획보고서 제출일",
            "당초 보고서 제출일", "철회관련 증권신고서 제출일",
            "관련 증권신고서 제출일",
        )
        label_reference = any(label in compact for label in explicit_labels)
        plan_date_match = re.search(
            r"거래계획보고서\s*제출일\s*[:：]?\s*"
            r"(20\d{2})\s*(?:년|[.\-/])\s*(\d{1,2})\s*(?:월|[.\-/])\s*(\d{1,2})\s*일?",
            compact,
        )
        explicit_plan_date = (
            f"{plan_date_match.group(1)}{int(plan_date_match.group(2)):02d}{int(plan_date_match.group(3)):02d}"
            if plan_date_match else None
        )
        compact_source_document = re.sub(r"\s+", "", source_document_text)
        source_has_explicit_plan_date = bool(
            explicit_plan_date
            and any(
                re.sub(r"\s+", "", value) in compact_source_document
                for value in _date_forms(explicit_plan_date)
            )
        )
        source_name = source.get("report_nm", "")
        follow_name = follow.get("report_nm", "")
        source_subjects = [
            part for part in re.findall(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)", source_name)
            if len(part) >= 8
        ]
        subject_reference = any(
            subject in follow_name or subject in compact for subject in source_subjects
        )
        is_trading_plan = "특정증권등거래계획보고서" in source_name
        if is_trading_plan:
            explicit = receipt_reference or (
                source_has_explicit_plan_date and label_reference
            )
        else:
            explicit = receipt_reference or (
                subject_reference and "철회" in follow_name
            )
        return {
            "source_receipt_number_in_document": receipt_reference,
            "source_date_in_document": date_reference,
            "explicit_source_field_label_in_document": label_reference,
            "unique_subject_reference": subject_reference,
            "explicit_plan_submission_date": explicit_plan_date,
            "source_document_has_explicit_plan_date": source_has_explicit_plan_date,
            "explicit_document_link": explicit,
            "evidence_snippet": _snippet(compact, (*explicit_labels, spec["source"])),
        }

    def probe_rm_withdrawal(self) -> dict[str, Any]:
        existing = self._existing_withdrawal_scan()
        list_cache: dict[tuple[str, str, str], tuple[dict[str, Any], str]] = {}
        cases: list[dict[str, Any]] = []
        for spec in WITHDRAWAL_SPECS:
            key = (spec["corp_code"], spec["source_dt"], spec["follow_dt"])
            if key not in list_cache:
                fixture = f"opendart/rm_withdrawal/{spec['group']}_{spec['corp_code']}_list.json"
                payload = self.api_list({
                    "corp_code": spec["corp_code"], "bgn_de": spec["source_dt"],
                    "end_de": spec["follow_dt"], "last_reprt_at": "N",
                }, fixture)
                list_cache[key] = (payload, fixture)
            payload, list_fixture = list_cache[key]
            rows = payload.get("list") or []
            source = next((row for row in rows if row.get("rcept_no") == spec["source"]), {})
            follow = next((row for row in rows if row.get("rcept_no") == spec["follow"]), {})
            document_fixture = f"opendart/rm_withdrawal/{spec['follow']}_document.zip"
            document_text = self.api_document(spec["follow"], document_fixture)
            source_document_fixture = None
            source_document_text = ""
            if spec["group"].startswith("new_pair"):
                source_document_fixture = (
                    f"opendart/rm_withdrawal/{spec['source']}_source_document.zip"
                )
                source_document_text = self.api_document(
                    spec["source"], source_document_fixture
                )
            link = self._explicit_withdrawal_link(
                spec, source, follow, document_text, source_document_text
            )
            list_pair_exact = bool(
                source and follow and "철" in (source.get("rm") or "")
                and "철회" in (follow.get("report_nm") or "")
                and source.get("corp_code") == follow.get("corp_code") == spec["corp_code"]
                and source.get("flr_nm") == follow.get("flr_nm") == spec["flr_nm"]
            )
            cases.append({
                **spec, "source_row": source, "follow_row": follow,
                "list_pair_exact": list_pair_exact, **link,
                "explicitly_linked": list_pair_exact and link["explicit_document_link"],
                "list_fixture": list_fixture, "document_fixture": document_fixture,
                "source_document_fixture": source_document_fixture,
            })
        linked = [item for item in cases if item["explicitly_linked"]]
        independent_keys = {
            (item["corp_code"], item["flr_nm"], item["source"]) for item in linked
        }
        new_linked = [item for item in linked if item["group"].startswith("new_pair")]
        count = len(independent_keys)
        status = "passed" if count >= 10 else (
            "partially_passed" if count else "unconfirmed"
        )
        return {
            "existing_stage0_scan": existing, "cases": cases,
            "strict_explicit_link_count": count,
            "strict_new_sample_count": len(new_linked),
            "target_for_confirmed": 10,
            "confidence": "confirmed" if count >= 10 else "provisional",
            "status": status,
        }

    def probe_rm_bond(self) -> dict[str, Any]:
        samples: dict[str, dict[str, Any]] = {}
        windows: list[dict[str, Any]] = []
        for start, end in BOND_WINDOWS:
            fixture = f"opendart/rm_bond/I006_{start}_{end}.json"
            payload = self.api_list({
                "bgn_de": start, "end_de": end, "last_reprt_at": "N",
                "pblntf_detail_ty": "I006",
            }, fixture)
            rows = payload.get("list") or []
            flagged: list[dict[str, Any]] = []
            for row in rows:
                rm = row.get("rm") or ""
                if "채" in rm:
                    parsed = {**row, "parsed_rm": parse_rm_flags(rm)}
                    samples[row["rcept_no"]] = parsed
                    flagged.append(parsed)
            windows.append({
                "start_date": start, "end_date": end,
                "status": payload.get("status"),
                "total_count": int(payload.get("total_count") or 0),
                "returned_row_count": len(rows),
                "rm_bond_row_count": len(flagged), "fixture": fixture,
            })
            if len(samples) >= 5:
                break
        values = list(samples.values())
        parser_preserved = all(
            item["parsed_rm"]["raw"] == item.get("rm", "") for item in values
        )
        combination_samples = [item for item in values if len(item.get("rm", "")) > 1]
        other_flags_preserved = bool(combination_samples) and all(
            all(
                char in item["parsed_rm"]["rm_flags"]
                for char in item.get("rm", "") if char in KNOWN_RM_FLAGS
            )
            for item in combination_samples
        )
        validated = (
            len(values) >= 3 and parser_preserved and other_flags_preserved
        )
        status = "passed" if validated else (
            "partially_passed" if values else "unconfirmed"
        )
        return {
            "target_detail_type": "I006", "target_detail_type_meaning": "채권공시",
            "official_rm_meaning": "채권상장법인", "windows": windows,
            "samples": values, "sample_count": len(values),
            "parser_preserved_raw_order": parser_preserved,
            "other_flags_preserved": other_flags_preserved,
            "combination_sample_count": len(combination_samples),
            "combination_parsing_status": (
                "passed" if other_flags_preserved else "unconfirmed"
            ),
            "confidence": "confirmed" if len(values) >= 3 else "provisional",
            "status": status,
        }

    def _write_golden(self) -> None:
        mapping = {
            "query_switch.json": self.findings["query_switch"],
            "page_size.json": self.findings["page_size"],
            "date_window.json": self.findings["date_window"],
            "rm_withdrawal.json": self.findings["rm_withdrawal"],
            "rm_bond.json": self.findings["rm_bond"],
            "stage0_6_findings.json": self.findings,
        }
        for name, value in mapping.items():
            _atomic_json(self.paths.golden_root / name, value)
        files = [
            {
                "path": str(path.relative_to(self.paths.repo_root)).replace("\\", "/"),
                "bytes": path.stat().st_size, "sha256": _sha256(path),
            }
            for path in sorted(self.paths.golden_root.glob("*.json"))
            if path.name != "manifest.json"
        ]
        golden_manifest = {
            "schema_version": 1, "stage": "0.6", "run_id": self.paths.run_id,
            "source_raw_manifest": str(
                (self.paths.raw_root / "manifest.json").relative_to(self.paths.repo_root)
            ).replace("\\", "/"),
            "files": files,
        }
        _atomic_json(self.paths.golden_root / "manifest.json", golden_manifest)
        self.manifest["golden_manifest"] = str(
            (self.paths.golden_root / "manifest.json").relative_to(self.paths.repo_root)
        ).replace("\\", "/")
        self.manifest["golden_files"] = files
        self.manifest["gate_summary"] = self.findings["gate_summary"]
        self._sync_manifests()


def rebuild_current_golden(repo_root: Path) -> None:
    """Recompute curated withdrawal evidence from an already recorded raw run."""
    root_manifest_path = repo_root / "stage0_6_manifest.json"
    manifest = json.loads(root_manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "completed" or manifest.get("stage") != "0.6":
        raise RuntimeError("current stage0_6 manifest is not a completed run")
    raw_root = repo_root / Path(manifest["raw_root"])
    golden_root = repo_root / Path(manifest["golden_root"])
    findings_path = golden_root / "stage0_6_findings.json"
    findings = json.loads(findings_path.read_text(encoding="utf-8"))
    withdrawal = findings["rm_withdrawal"]
    for case in withdrawal["cases"]:
        follow_text = _zip_text((raw_root / Path(case["document_fixture"])).read_bytes())
        source_text = ""
        if case.get("source_document_fixture"):
            source_text = _zip_text(
                (raw_root / Path(case["source_document_fixture"])).read_bytes()
            )
        link = Stage06Probe._explicit_withdrawal_link(
            None, case, case["source_row"], case["follow_row"], follow_text, source_text
        )
        case.update(link)
        case["explicitly_linked"] = bool(
            case["list_pair_exact"] and link["explicit_document_link"]
        )
    linked = [item for item in withdrawal["cases"] if item["explicitly_linked"]]
    independent = {
        (item["corp_code"], item["flr_nm"], item["source"]) for item in linked
    }
    count = len(independent)
    withdrawal["strict_explicit_link_count"] = count
    withdrawal["strict_new_sample_count"] = sum(
        item["explicitly_linked"] and item["group"].startswith("new_pair")
        for item in withdrawal["cases"]
    )
    withdrawal["confidence"] = "confirmed" if count >= 10 else "provisional"
    withdrawal["status"] = "passed" if count >= 10 else (
        "partially_passed" if count else "unconfirmed"
    )
    findings["gate_summary"]["GATE-RM-WITHDRAWAL"] = withdrawal["status"]

    bond = findings["rm_bond"]
    combination_samples = [
        item for item in bond["samples"] if len(item.get("rm", "")) > 1
    ]
    bond["combination_sample_count"] = len(combination_samples)
    bond["other_flags_preserved"] = bool(combination_samples) and all(
        all(
            char in item["parsed_rm"]["rm_flags"]
            for char in item.get("rm", "") if char in KNOWN_RM_FLAGS
        )
        for item in combination_samples
    )
    bond["combination_parsing_status"] = (
        "passed" if bond["other_flags_preserved"] else "unconfirmed"
    )
    bond["status"] = (
        "passed" if bond["sample_count"] >= 3 and bond["other_flags_preserved"]
        else "partially_passed" if bond["sample_count"] else "unconfirmed"
    )
    findings["gate_summary"]["GATE-RM-BOND"] = bond["status"]
    _atomic_json(golden_root / "rm_withdrawal.json", withdrawal)
    _atomic_json(golden_root / "rm_bond.json", bond)
    _atomic_json(findings_path, findings)

    files = [
        {
            "path": str(path.relative_to(repo_root)).replace("\\", "/"),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(golden_root.glob("*.json"))
        if path.name != "manifest.json"
    ]
    golden_manifest = {
        "schema_version": 1,
        "stage": "0.6",
        "run_id": manifest["run_id"],
        "source_raw_manifest": str(
            (raw_root / "manifest.json").relative_to(repo_root)
        ).replace("\\", "/"),
        "files": files,
    }
    _atomic_json(golden_root / "manifest.json", golden_manifest)
    manifest["golden_files"] = files
    manifest["gate_summary"] = findings["gate_summary"]
    manifest["golden_rebuilt_at"] = utc_now()
    _atomic_json(raw_root / "manifest.json", manifest)
    _atomic_json(root_manifest_path, manifest)
