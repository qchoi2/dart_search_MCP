from __future__ import annotations

import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import signal
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .common import RecordedHttpClient, sha256_bytes, utc_now, write_json
from .dart_web import DART_BASE, _base_form, parse_search_html


API_BASE = "https://opendart.fss.or.kr/api"
RM_GUIDE_URL = "https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001"
TERMS = (
    "상계납입",
    "상계 납입",
    "주금납입채무와 상계",
    "주금 납입 채무와 상계",
    "출자전환",
    "채권의 출자전환",
)
RM_FLAGS = ("유", "코", "채", "넥", "공", "연")


def _safe(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", value).strip("_")[:100]


def _json_loads(body: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label}: OpenDART response was not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}: OpenDART response was not an object")
    return value


def _strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _zip_text(body: bytes) -> str:
    if not body.startswith(b"PK"):
        return ""
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        parts: list[str] = []
        for info in archive.infolist():
            if info.is_dir() or info.file_size > 30_000_000:
                continue
            raw = archive.read(info)
            decoded = None
            for encoding in ("utf-8", "euc-kr", "cp949"):
                try:
                    decoded = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    pass
            if decoded is not None:
                parts.append(decoded)
    return _strip_tags("\n".join(parts))


def _snippets(text: str, needles: Iterable[str], radius: int = 500) -> list[str]:
    result: list[str] = []
    compact = re.sub(r"\s+", " ", text)
    for needle in needles:
        match = re.search(re.escape(needle), compact, re.IGNORECASE)
        if match:
            start = max(0, match.start() - radius)
            end = min(len(compact), match.end() + radius)
            result.append(compact[start:end])
    deduped: list[str] = []
    for item in result:
        if item not in deduped:
            deduped.append(item)
    return deduped[:4]


def _normalized_report_name(value: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"^\s*(?:\[[^]]+\]\s*)+", "", value))


def _status(
    *,
    passed: bool,
    evidence_count: int = 0,
    partial: bool | None = None,
) -> str:
    if passed:
        return "passed"
    if partial is None:
        partial = evidence_count > 0
    return "partially_passed" if partial else "unconfirmed"


@dataclass(frozen=True)
class Stage05Paths:
    repo_root: Path
    fixture_root: Path
    run_id: str

    @property
    def raw_root(self) -> Path:
        return self.fixture_root / "raw" / self.run_id

    @property
    def golden_root(self) -> Path:
        return self.fixture_root / "golden" / "stage0_5"

    @property
    def lock_path(self) -> Path:
        return self.fixture_root / ".stage0_5.lock"


class Stage05Probe:
    def __init__(
        self,
        *,
        repo_root: Path,
        api_key: str,
        run_id: str,
        max_requests: int,
        deadline_seconds: int,
        min_interval: float,
    ) -> None:
        fixture_root = repo_root / "tests" / "fixtures" / "probe"
        self.paths = Stage05Paths(repo_root, fixture_root, run_id)
        self.paths.raw_root.mkdir(parents=True, exist_ok=False)
        self.paths.golden_root.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key
        self.max_requests = max_requests
        self.deadline_seconds = deadline_seconds
        self.started_monotonic = time.monotonic()
        self.deadline_monotonic = self.started_monotonic + deadline_seconds
        self.cancel_path = self.paths.raw_root / "CANCEL"
        self.http = RecordedHttpClient(
            self.paths.raw_root,
            min_interval=min_interval,
            max_requests=max_requests,
            deadline_monotonic=self.deadline_monotonic,
            cancel_path=self.cancel_path,
        )
        self._lock_fd: int | None = None
        self._interrupted = False
        self.findings: dict[str, Any] = {}
        self.manifest: dict[str, Any] = {
            "schema_version": 1,
            "stage": "0.5",
            "run_id": run_id,
            "status": "running",
            "started_at": utc_now(),
            "pid": os.getpid(),
            "concurrency": 1,
            "configured_min_interval_seconds": min_interval,
            "request_limit": max_requests,
            "deadline_seconds": deadline_seconds,
            "deadline_at": (
                datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)
            ).isoformat(timespec="seconds"),
            "api_key": "***MASKED***",
            "cookies_recorded": False,
            "raw_root": str(self.paths.raw_root.relative_to(repo_root)),
            "golden_root": str(self.paths.golden_root.relative_to(repo_root)),
            "stop_reason": None,
        }

    def __enter__(self) -> "Stage05Probe":
        try:
            self._lock_fd = os.open(
                self.paths.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            raise RuntimeError(f"probe lock already exists: {self.paths.lock_path}") from exc
        os.write(self._lock_fd, f"{os.getpid()} {self.paths.run_id}\n".encode("ascii"))
        self._write_run_manifest()
        signal.signal(signal.SIGINT, self._signal_stop)
        signal.signal(signal.SIGTERM, self._signal_stop)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc is None:
            self.manifest["status"] = "completed"
            self.manifest["stop_reason"] = "completed_stage0_5_only"
        else:
            self.manifest["status"] = "stopped"
            self.manifest["stop_reason"] = str(exc)
        self.manifest["finished_at"] = utc_now()
        self.manifest["elapsed_seconds"] = round(time.monotonic() - self.started_monotonic, 3)
        self.manifest["request_count"] = len(self.http.records)
        self.manifest["request_limit_remaining"] = self.max_requests - len(self.http.records)
        self.manifest["child_processes_started"] = 0
        self.manifest["main_development_started"] = False
        self._write_run_manifest()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self.paths.lock_path.unlink(missing_ok=True)

    def _signal_stop(self, signum: int, _frame: Any) -> None:
        self._interrupted = True
        self.cancel_path.write_text(f"signal={signum}\n", encoding="ascii")

    def _write_run_manifest(self) -> None:
        temp = self.paths.raw_root / "manifest.json.tmp"
        write_json(temp, self.manifest)
        temp.replace(self.paths.raw_root / "manifest.json")

    def api_list(self, params: dict[str, Any], fixture: str) -> dict[str, Any]:
        response = self.http.request(
            "GET",
            f"{API_BASE}/list.json",
            params={
                "crtfc_key": self.api_key,
                "sort": "date",
                "sort_mth": "desc",
                "page_no": 1,
                "page_count": 100,
                **params,
            },
            fixture=fixture,
            timeout=90,
        )
        payload = _json_loads(response.body, fixture)
        if payload.get("status") not in {"000", "013"}:
            raise RuntimeError(
                f"{fixture}: OpenDART status={payload.get('status')} message={payload.get('message')}"
            )
        return payload

    def api_document(self, rcept_no: str, fixture: str) -> tuple[bytes, str]:
        response = self.http.request(
            "GET",
            f"{API_BASE}/document.xml",
            params={"crtfc_key": self.api_key, "rcept_no": rcept_no},
            fixture=fixture,
            timeout=120,
        )
        return response.body, _zip_text(response.body)

    def dart_search(
        self,
        query: str,
        *,
        page: int = 1,
        mode_post: bool,
        fixture_stem: str,
        start_date: str = "20250716",
        end_date: str = "20260716",
    ) -> dict[str, Any]:
        form = _base_form(start_date, end_date)
        form.update(
            {
                "currentPage": str(page),
                "option": "contents",
                "keyword": query,
                "b_keyword": query,
            }
        )
        if mode_post:
            self.http.request(
                "POST",
                f"{DART_BASE}/dsab007/detailSearchMain2.do",
                form=form,
                headers={"Referer": f"{DART_BASE}/dsab007/main.do"},
                fixture=f"dart/{fixture_stem}_mode.html",
                timeout=90,
            )
        response = self.http.request(
            "POST",
            f"{DART_BASE}/dsab007/search.ax",
            form=form,
            headers={"Referer": f"{DART_BASE}/dsab007/main.do"},
            fixture=f"dart/{fixture_stem}.html",
            timeout=90,
        )
        parsed = parse_search_html(response.text)
        parsed.update(
            {
                "query": query,
                "page": page,
                "mode_post": mode_post,
                "fixture": response.fixture,
                "http_status": response.status,
                "page_links": sorted(
                    {int(value) for value in re.findall(r"search\(\s*(\d+)\s*\)", response.text)}
                ),
            }
        )
        return parsed

    def run(self) -> dict[str, Any]:
        self.findings = {
            "schema_version": 1,
            "scope": "DEVELOPMENT_PLAN v16 stage 0.5 measurement only",
            "run_id": self.paths.run_id,
            "measured_at": utc_now(),
            "flagship_terms": self.probe_flagship_terms(),
        }
        self.findings["dart_pagination"] = self.probe_pagination(
            self.findings["flagship_terms"]
        )
        self.findings["amendment_strata"] = self.probe_amendment_strata()
        self.findings["rm_market"] = self.probe_rm_market()
        self.findings["d004_equal_filer"] = self.probe_d004_equal_filer()
        self.findings["gate_summary"] = {
            "GATE-FLAGSHIP-TERMS": self.findings["flagship_terms"]["gate_status"],
            "GATE-DART-PAGINATION": self.findings["dart_pagination"]["gate_status"],
            "GATE-AMENDMENT-STRATA": self.findings["amendment_strata"]["gate_status"],
            "GATE-RM-MARKET": self.findings["rm_market"]["gate_status"],
            "GATE-D004-EQUAL-FILER": self.findings["d004_equal_filer"]["gate_status"],
        }
        self.findings["request_count"] = len(self.http.records)
        self.findings["request_limit"] = self.max_requests
        self.findings["main_development_started"] = False
        self._write_golden_and_reports()
        return self.findings

    def probe_flagship_terms(self) -> dict[str, Any]:
        searches: list[dict[str, Any]] = []
        cumulative: set[str] = set()
        for index, query in enumerate(TERMS, start=1):
            result = self.dart_search(
                query,
                mode_post=True,
                fixture_stem=f"terms/{index:02d}_{_safe(query)}_page_001",
            )
            receipts = [row["rcept_no"] for row in result["rows"] if row.get("rcept_no")]
            unique = set(receipts)
            new = unique - cumulative
            cumulative.update(unique)
            searches.append(
                {
                    "query": query,
                    "classification": result["classification"],
                    "result_count": result["result_count"],
                    "effective_page_size": len(receipts),
                    "first_page_receipts": receipts,
                    "within_page_duplicate_count": len(receipts) - len(unique),
                    "within_page_duplicate_rate": (
                        (len(receipts) - len(unique)) / len(receipts) if receipts else 0.0
                    ),
                    "new_candidate_receipts": sorted(new),
                    "new_candidate_contribution_count": len(new),
                    "new_candidate_contribution_rate": len(new) / len(unique) if unique else 0.0,
                    "page_links": result["page_links"],
                    "raw_fixture": result["fixture"],
                }
            )

        candidates: list[tuple[str, str]] = []
        for search in searches:
            for receipt in search["first_page_receipts"][:2]:
                pair = (receipt, search["query"])
                if receipt not in {item[0] for item in candidates}:
                    candidates.append(pair)
        for search in searches:
            for receipt in search["first_page_receipts"]:
                pair = (receipt, search["query"])
                if receipt not in {item[0] for item in candidates}:
                    candidates.append(pair)

        validations: list[dict[str, Any]] = []
        for receipt, source_query in candidates[:12]:
            fixture = f"opendart/flagship/documents/{receipt}.zip"
            body, text = self.api_document(receipt, fixture)
            needles = TERMS + ("현물출자", "주금납입채무", "상계", "출자 전환")
            snippets = _snippets(text, needles)
            compact = re.sub(r"\s+", "", text)
            direct_setoff = (
                "상계납입" in compact
                or ("주금납입채무" in compact and "상계" in compact)
            )
            debt_equity = "출자전환" in compact or "채권의출자전환" in compact
            in_kind = "현물출자" in compact
            if direct_setoff:
                classification = "direct_setoff_payment"
            elif debt_equity:
                classification = "debt_equity_conversion"
            elif in_kind:
                classification = "in_kind_contribution"
            elif "상계" in compact:
                classification = "other_or_accounting_setoff"
            else:
                classification = "query_term_not_found_in_downloaded_document"
            snippet_path = self.paths.raw_root / f"opendart/flagship/snippets/{receipt}.txt"
            snippet_path.parent.mkdir(parents=True, exist_ok=True)
            snippet_path.write_text("\n\n".join(snippets) + "\n", encoding="utf-8")
            validations.append(
                {
                    "rcept_no": receipt,
                    "source_query": source_query,
                    "document_is_zip": body.startswith(b"PK"),
                    "classification": classification,
                    "term_found": bool(snippets),
                    "raw_document_fixture": fixture,
                    "raw_snippet_fixture": str(snippet_path.relative_to(self.paths.raw_root)),
                    "snippet_sha256": sha256_bytes(snippet_path.read_bytes()),
                }
            )
            direct_count = sum(
                item["classification"] == "direct_setoff_payment" for item in validations
            )
            conversion_count = sum(
                item["classification"] == "debt_equity_conversion" for item in validations
            )
            if (
                len([item for item in validations if item["term_found"]]) >= 5
                and direct_count >= 2
                and conversion_count >= 2
            ):
                break

        measured = all(item["result_count"] is not None for item in searches)
        verified_count = sum(1 for item in validations if item["term_found"])
        useful_count = sum(
            1
            for item in validations
            if item["classification"]
            in {"direct_setoff_payment", "debt_equity_conversion", "in_kind_contribution"}
        )
        classification_counts = {
            classification: sum(
                item["classification"] == classification for item in validations
            )
            for classification in (
                "direct_setoff_payment",
                "debt_equity_conversion",
                "in_kind_contribution",
                "other_or_accounting_setoff",
                "query_term_not_found_in_downloaded_document",
            )
        }
        return {
            "queries": searches,
            "source_scope": "first response page per query; duplicate and contribution rates are first-page rates",
            "document_validations": validations,
            "document_verified_count": verified_count,
            "semantically_useful_document_count": useful_count,
            "classification_counts": classification_counts,
            "gate_status": _status(
                passed=(
                    measured
                    and verified_count >= 5
                    and classification_counts["direct_setoff_payment"] >= 2
                    and classification_counts["debt_equity_conversion"] >= 2
                ),
                evidence_count=verified_count,
            ),
        }

    def probe_pagination(self, flagship: dict[str, Any]) -> dict[str, Any]:
        positive = [
            item
            for item in flagship["queries"]
            if isinstance(item.get("result_count"), int) and item["result_count"] > 0
        ]
        if not positive:
            return {
                "gate_status": "unconfirmed",
                "reason": "No positive flagship query was available for pagination",
            }
        widest = max(positive, key=lambda item: item["result_count"])
        narrowest = min(positive, key=lambda item: item["result_count"])
        cases: list[dict[str, Any]] = []
        for label, seed in (("wide", widest), ("narrow", narrowest)):
            effective = seed["effective_page_size"]
            if not effective:
                continue
            last_page = max(1, math.ceil(seed["result_count"] / effective))
            page2 = self.dart_search(
                seed["query"],
                page=2,
                mode_post=False,
                fixture_stem=f"pagination/{label}_{_safe(seed['query'])}_page_002_direct",
            )
            last = self.dart_search(
                seed["query"],
                page=last_page,
                mode_post=False,
                fixture_stem=f"pagination/{label}_{_safe(seed['query'])}_page_{last_page:06d}",
            )
            empty = self.dart_search(
                seed["query"],
                page=last_page + 1,
                mode_post=False,
                fixture_stem=f"pagination/{label}_{_safe(seed['query'])}_page_{last_page + 1:06d}_empty",
            )
            overflow = self.dart_search(
                seed["query"],
                page=last_page + 100,
                mode_post=False,
                fixture_stem=f"pagination/{label}_{_safe(seed['query'])}_page_{last_page + 100:06d}_overflow",
            )
            page1_receipts = seed["first_page_receipts"]
            page2_receipts = [row["rcept_no"] for row in page2["rows"]]
            overlap = sorted(set(page1_receipts) & set(page2_receipts))
            cases.append(
                {
                    "label": label,
                    "query": seed["query"],
                    "result_count": seed["result_count"],
                    "requested_max_results": 100,
                    "effective_page_size": effective,
                    "calculated_last_page": last_page,
                    "page_1_receipts": page1_receipts,
                    "page_2_receipts": page2_receipts,
                    "page_1_2_overlap": overlap,
                    "page_1_2_nonoverlap": not overlap and bool(page2_receipts),
                    "page_2_without_mode_post": page2["classification"] == "results",
                    "last_page_row_count": len(last["rows"]),
                    "last_page_classification": last["classification"],
                    "empty_page_number": last_page + 1,
                    "empty_page_classification": empty["classification"],
                    "empty_page_row_count": len(empty["rows"]),
                    "overflow_page_number": last_page + 100,
                    "overflow_page_classification": overflow["classification"],
                    "overflow_page_row_count": len(overflow["rows"]),
                    "raw_fixtures": [
                        page2["fixture"],
                        last["fixture"],
                        empty["fixture"],
                        overflow["fixture"],
                    ],
                }
            )
        passed = bool(cases) and all(
            case["page_1_2_nonoverlap"]
            and case["page_2_without_mode_post"]
            and case["last_page_row_count"] > 0
            and case["empty_page_row_count"] == 0
            and case["overflow_page_row_count"] == 0
            for case in cases
        )
        return {
            "cases": cases,
            "gate_status": _status(passed=passed, evidence_count=len(cases)),
            "session_reuse_note": "page 2, last, empty, and overflow calls omitted the mode-setting POST in the same cookie session",
        }

    def probe_amendment_strata(self) -> dict[str, Any]:
        strata = (
            {
                "name": "유상증자",
                "corp_code": "00232007",
                "corp_name": "상지건설",
                "bgn_de": "20260508",
                "end_de": "20260716",
                "normalized": "주요사항보고서(유상증자결정)",
                "chain": [
                    "20260508000511",
                    "20260605000403",
                    "20260619000662",
                    "20260625000325",
                    "20260716000809",
                ],
            },
            {
                "name": "합병",
                "corp_code": "00133618",
                "corp_name": "세기상사",
                "bgn_de": "20260521",
                "end_de": "20260713",
                "normalized": "주요사항보고서(회사합병결정)",
                "chain": [
                    "20260521000642",
                    "20260617000483",
                    "20260623000277",
                    "20260713000345",
                ],
            },
            {
                "name": "전환사채",
                "corp_code": "00406037",
                "corp_name": "CSA 코스믹",
                "bgn_de": "20260611",
                "end_de": "20260715",
                "normalized": "주요사항보고서(전환사채권발행결정)",
                "chain": ["20260611000640", "20260710000465", "20260715000496"],
            },
        )
        results: list[dict[str, Any]] = []
        for item in strata:
            base = {
                "corp_code": item["corp_code"],
                "bgn_de": item["bgn_de"],
                "end_de": item["end_de"],
                "pblntf_ty": "B",
            }
            n_payload = self.api_list(
                {**base, "last_reprt_at": "N"},
                f"opendart/amendment/{_safe(item['name'])}_N.json",
            )
            y_payload = self.api_list(
                {**base, "last_reprt_at": "Y"},
                f"opendart/amendment/{_safe(item['name'])}_Y.json",
            )
            n_rows = [
                row
                for row in n_payload.get("list") or []
                if _normalized_report_name(row.get("report_nm", "")) == item["normalized"]
            ]
            y_rows = [
                row
                for row in y_payload.get("list") or []
                if _normalized_report_name(row.get("report_nm", "")) == item["normalized"]
            ]
            expected = item["chain"]
            n_receipts = [row["rcept_no"] for row in n_rows if row["rcept_no"] in expected]
            y_receipts = [row["rcept_no"] for row in y_rows if row["rcept_no"] in expected]
            passed = set(n_receipts) == set(expected) and y_receipts == [expected[-1]]
            results.append(
                {
                    **item,
                    "N_chain_rows": [row for row in n_rows if row["rcept_no"] in expected],
                    "Y_chain_rows": [row for row in y_rows if row["rcept_no"] in expected],
                    "N_receipts": n_receipts,
                    "Y_receipts": y_receipts,
                    "original_receipt": expected[0],
                    "intermediate_receipts": expected[1:-1],
                    "final_receipt": expected[-1],
                    "chain_passed": passed,
                    "raw_fixtures": [
                        f"opendart/amendment/{_safe(item['name'])}_N.json",
                        f"opendart/amendment/{_safe(item['name'])}_Y.json",
                    ],
                }
            )

        independent_specs = (
            {
                "name": "아이엠증권 회귀",
                "corp_code": "00148665",
                "bgn_de": "20260609",
                "end_de": "20260815",
                "pblntf_ty": "B",
                "normalized": "주요사항보고서(자본으로인정되는채무증권발행결정)",
                "chain_final": "20260716000411",
                "outside": "20260709000043",
            },
            {
                "name": "우성머티리얼스 유상증자 다중사건",
                "corp_code": "00132868",
                "bgn_de": "20240801",
                "end_de": "20241231",
                "pblntf_ty": "B",
                "normalized": "주요사항보고서(유상증자결정)",
            },
            {
                "name": "알엔티엑스 전환사채 다중사건",
                "corp_code": "00615723",
                "bgn_de": "20260501",
                "end_de": "20260716",
                "pblntf_ty": "B",
                "normalized": "주요사항보고서(전환사채권발행결정)",
            },
        )
        independent: list[dict[str, Any]] = []
        for index, spec in enumerate(independent_specs, start=1):
            payload = self.api_list(
                {
                    "corp_code": spec["corp_code"],
                    "bgn_de": spec["bgn_de"],
                    "end_de": spec["end_de"],
                    "pblntf_ty": spec["pblntf_ty"],
                    "last_reprt_at": "Y",
                },
                f"opendart/amendment/independent_{index:02d}_Y.json",
            )
            rows = [
                row
                for row in payload.get("list") or []
                if _normalized_report_name(row.get("report_nm", "")) == spec["normalized"]
            ]
            receipts = [row["rcept_no"] for row in rows]
            if "outside" in spec:
                confirmed = spec["chain_final"] in receipts and spec["outside"] in receipts
            else:
                confirmed = len(set(receipts)) >= 2
            independent.append(
                {
                    **spec,
                    "Y_rows": rows,
                    "Y_receipts": receipts,
                    "independent_events_preserved": confirmed,
                    "raw_fixture": f"opendart/amendment/independent_{index:02d}_Y.json",
                }
            )
        im_case = independent[0]
        im_assertion = (
            im_case["outside"] in im_case["Y_receipts"]
            and im_case["chain_final"] in im_case["Y_receipts"]
            and im_case["outside"] != im_case["chain_final"]
        )
        passed = all(item["chain_passed"] for item in results) and all(
            item["independent_events_preserved"] for item in independent
        ) and im_assertion
        return {
            "strata": results,
            "independent_event_samples": independent,
            "im_securities_regression_assertion": im_assertion,
            "gate_status": _status(
                passed=passed,
                evidence_count=sum(item["chain_passed"] for item in results),
            ),
        }

    def probe_rm_market(self) -> dict[str, Any]:
        guide = self.http.request(
            "GET",
            RM_GUIDE_URL,
            fixture="opendart/rm_market/official_list_api_guide.html",
            timeout=90,
        )
        guide_text = _strip_tags(guide.text)
        official_labels = {
            "유": "유가증권시장",
            "코": "코스닥시장",
            "채": "채권상장법인",
            "넥": "코넥스시장",
            "공": "공정거래위원회",
            "연": "연결",
        }
        samples: dict[str, list[dict[str, Any]]] = {flag: [] for flag in RM_FLAGS}
        combinations: set[str] = set()
        scans = (
            ("I", "20260416", "20260716", 12),
            ("A", "20260416", "20260716", 8),
            ("B", "20260416", "20260716", 8),
            ("I", "20260101", "20260331", 8),
        )
        raw_fixtures: list[str] = []
        for pblntf_ty, bgn_de, end_de, max_pages in scans:
            for page_no in range(1, max_pages + 1):
                fixture = (
                    f"opendart/rm_market/{pblntf_ty}_{bgn_de}_{end_de}_page_{page_no:03d}.json"
                )
                payload = self.api_list(
                    {
                        "bgn_de": bgn_de,
                        "end_de": end_de,
                        "last_reprt_at": "N",
                        "pblntf_ty": pblntf_ty,
                        "page_no": page_no,
                    },
                    fixture,
                )
                raw_fixtures.append(fixture)
                rows = payload.get("list") or []
                for row in rows:
                    rm = row.get("rm") or ""
                    if rm:
                        combinations.add(rm)
                    for flag in RM_FLAGS:
                        if flag in rm and len(samples[flag]) < 5:
                            samples[flag].append(row)
                if page_no >= int(payload.get("total_page") or 1):
                    break
                if all(samples[flag] for flag in RM_FLAGS):
                    break
            if all(samples[flag] for flag in RM_FLAGS):
                break
        # `채` is rare in date-sorted general pages.  Probe known bond-issuing
        # public entities and banks by corp_code rather than extrapolating from
        # their company names.
        if not samples["채"]:
            bond_candidate_corps = (
                ("00382001", "한국수력원자력"),
                ("00311252", "한국도로공사"),
                ("00783787", "한국토지주택공사"),
                ("00104476", "국민은행"),
                ("00137571", "신한은행"),
            )
            for index, (corp_code, corp_name) in enumerate(bond_candidate_corps, start=1):
                fixture = f"opendart/rm_market/bond_candidate_{index:02d}_{corp_code}.json"
                payload = self.api_list(
                    {
                        "corp_code": corp_code,
                        "bgn_de": "20260101",
                        "end_de": "20260716",
                        "last_reprt_at": "N",
                    },
                    fixture,
                )
                raw_fixtures.append(fixture)
                for row in payload.get("list") or []:
                    rm = row.get("rm") or ""
                    if rm:
                        combinations.add(rm)
                    for flag in RM_FLAGS:
                        if flag in rm and len(samples[flag]) < 5:
                            samples[flag].append(row)
                if samples["채"]:
                    break
        definitions = {
            flag: {
                "label": label,
                "label_observed_in_official_guide_response": label in guide_text,
                "sample_count": len(samples[flag]),
                "confidence": (
                    "confirmed"
                    if label in guide_text and samples[flag]
                    else "provisional"
                ),
            }
            for flag, label in official_labels.items()
        }
        confirmed = [flag for flag, item in definitions.items() if item["confidence"] == "confirmed"]
        return {
            "official_guide_url": RM_GUIDE_URL,
            "official_guide_fixture": guide.fixture,
            "definitions": definitions,
            "samples": samples,
            "observed_rm_combinations": sorted(combinations),
            "raw_list_fixtures": raw_fixtures,
            "confirmed_flags": confirmed,
            "provisional_flags": [flag for flag in RM_FLAGS if flag not in confirmed],
            "gate_status": _status(
                passed=len(confirmed) == len(RM_FLAGS),
                evidence_count=len(confirmed),
            ),
        }

    def probe_d004_equal_filer(self) -> dict[str, Any]:
        payloads = [
            self.api_list(
                {
                    "bgn_de": "20260401",
                    "end_de": "20260630",
                    "last_reprt_at": "N",
                    "pblntf_detail_ty": "D004",
                },
                "opendart/d004_equal/window_20260401_20260630.json",
            ),
            self.api_list(
                {
                    "bgn_de": "20260701",
                    "end_de": "20260716",
                    "last_reprt_at": "N",
                    "pblntf_detail_ty": "D004",
                },
                "opendart/d004_equal/window_20260701_20260716.json",
            ),
        ]
        rows: dict[str, dict[str, Any]] = {}
        for payload in payloads:
            for row in payload.get("list") or []:
                if row.get("corp_name") == row.get("flr_nm"):
                    rows[row["rcept_no"]] = row
        role_results: list[dict[str, Any]] = []
        for receipt, row in sorted(rows.items())[:10]:
            fixture = f"opendart/d004_equal/documents/{receipt}.zip"
            body, text = self.api_document(receipt, fixture)
            report_name = row.get("report_nm", "")
            if "의견표명" in report_name:
                role = "target_company_opinion"
            elif "결과보고" in report_name:
                role = "tender_offer_result"
            elif "설명서" in report_name:
                role = "tender_offer_explanatory_statement"
            elif "신고서" in report_name:
                role = "tender_offer_filing"
            else:
                role = "unclassified"
            snippets = _snippets(
                text,
                ("공개매수 대상회사", "대상회사", "공개매수자", "공개매수인", row["corp_name"]),
                radius=700,
            )
            snippet_path = self.paths.raw_root / f"opendart/d004_equal/snippets/{receipt}.txt"
            snippet_path.parent.mkdir(parents=True, exist_ok=True)
            snippet_path.write_text("\n\n".join(snippets) + "\n", encoding="utf-8")
            compact = re.sub(r"\s+", "", text)
            corp_present = row["corp_name"].replace(" ", "") in compact
            target_label_present = "대상회사" in compact or "공개매수대상회사" in compact
            role_results.append(
                {
                    "list_row": row,
                    "role": role,
                    "document_is_zip": body.startswith(b"PK"),
                    "corp_name_present_in_document": corp_present,
                    "target_label_present": target_label_present,
                    "raw_document_fixture": fixture,
                    "raw_snippet_fixture": str(snippet_path.relative_to(self.paths.raw_root)),
                }
            )
        roles = sorted({item["role"] for item in role_results})
        verified = [
            item
            for item in role_results
            if item["document_is_zip"] and item["corp_name_present_in_document"]
        ]
        all_four_roles = {
            "target_company_opinion",
            "tender_offer_result",
            "tender_offer_explanatory_statement",
            "tender_offer_filing",
        }.issubset(roles)
        # The list equality alone is never treated as enough to infer target role.
        rule = (
            "report-role-first: opinion filings identify corp_name as the opinion-giving target; "
            "filing/explanatory/result documents require their explicit target-company field"
            if verified
            else "unconfirmed"
        )
        passed = len(verified) >= 4 and all_four_roles and all(
            item["target_label_present"] for item in verified if item["role"] != "target_company_opinion"
        )
        return {
            "equal_filer_list_count": len(rows),
            "documents": role_results,
            "roles_observed": roles,
            "document_verified_count": len(verified),
            "target_company_rule": rule,
            "rule_can_be_finalized": passed,
            "gate_status": _status(passed=passed, evidence_count=len(verified)),
            "raw_list_fixtures": [
                "opendart/d004_equal/window_20260401_20260630.json",
                "opendart/d004_equal/window_20260701_20260716.json",
            ],
        }

    def _write_golden_and_reports(self) -> None:
        gate_map = {
            "flagship_terms": self.findings["flagship_terms"],
            "dart_pagination": self.findings["dart_pagination"],
            "amendment_strata": self.findings["amendment_strata"],
            "rm_market": self.findings["rm_market"],
            "d004_equal_filer": self.findings["d004_equal_filer"],
        }
        for name, value in gate_map.items():
            write_json(self.paths.golden_root / f"{name}.json", value)
        write_json(self.paths.golden_root / "stage0_5_findings.json", self.findings)
        manifest_files: list[dict[str, Any]] = []
        for path in sorted(self.paths.golden_root.glob("*.json")):
            if path.name == "manifest.json":
                continue
            manifest_files.append(
                {
                    "path": str(path.relative_to(self.paths.fixture_root)),
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        golden_manifest = {
            "schema_version": 1,
            "stage": "0.5",
            "run_id": self.paths.run_id,
            "generated_at": utc_now(),
            "source_raw_manifest": str(
                (self.paths.raw_root / "manifest.json").relative_to(self.paths.fixture_root)
            ),
            "files": manifest_files,
        }
        write_json(self.paths.golden_root / "manifest.json", golden_manifest)
        render_stage05_reports(self.paths.repo_root, self.findings, self.manifest)


def _table_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_stage05_reports(
    repo_root: Path,
    findings: dict[str, Any],
    run_manifest: dict[str, Any],
) -> None:
    lines = [
        "# 단계 0.5 실측 결과",
        "",
        f"- run_id: `{findings['run_id']}`",
        f"- 실측시각(UTC): `{findings['measured_at']}`",
        f"- 요청: `{findings['request_count']} / {findings['request_limit']}`",
        "- 동시성: `1`",
        "- API 키·쿠키: 요청 로그에서 마스킹 또는 미기록",
        "- 범위: 단계 0.5 실측만 수행, 단계 1 본개발 미착수",
        "",
        "## 게이트 판정",
        "",
        "| 게이트 | 판정 |",
        "|---|---|",
    ]
    for gate, status in findings["gate_summary"].items():
        lines.append(f"| `{gate}` | `{status}` |")

    flagship = findings["flagship_terms"]
    lines.extend(
        [
            "",
            "## 1. GATE-FLAGSHIP-TERMS",
            "",
            "| 질의 | 검색건수 | 첫 페이지 | 중복률 | 신규 후보 기여 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for item in flagship["queries"]:
        lines.append(
            f"| {_table_escape(item['query'])} | {item['result_count']} | "
            f"{item['effective_page_size']} | {item['within_page_duplicate_rate']:.1%} | "
            f"{item['new_candidate_contribution_count']} ({item['new_candidate_contribution_rate']:.1%}) |"
        )
    lines.extend(["", "### 첫 페이지 접수번호", ""])
    for item in flagship["queries"]:
        lines.append(
            f"- `{item['query']}`: `{', '.join(item['first_page_receipts']) or '없음'}`"
        )
    lines.extend(
        [
            "",
            f"원문에서 검색 문구를 확인한 표본은 {flagship['document_verified_count']}건이며, "
            f"상계납입·출자전환·현물출자로 분류 가능한 표본은 {flagship['semantically_useful_document_count']}건이다.",
            f"분류별 표본 수: `{json.dumps(flagship['classification_counts'], ensure_ascii=False, sort_keys=True)}`",
            "중복률과 신규 기여도는 각 질의의 첫 페이지 접수번호를 기준으로 계산했다.",
            "",
            "### 원문 검증",
            "",
            "| 접수번호 | 유입 질의 | 분류 | 원문 문구 확인 |",
            "|---|---|---|---|",
        ]
    )
    for item in flagship["document_validations"]:
        lines.append(
            f"| `{item['rcept_no']}` | {_table_escape(item['source_query'])} | "
            f"`{item['classification']}` | {item['term_found']} |"
        )

    pagination = findings["dart_pagination"]
    lines.extend(["", "## 2. GATE-DART-PAGINATION", ""])
    for case in pagination.get("cases", []):
        lines.extend(
            [
                f"### {case['label']} — `{case['query']}`",
                "",
                f"- 검색건수 {case['result_count']}, effective_page_size {case['effective_page_size']}, 마지막 페이지 {case['calculated_last_page']}",
                f"- 1·2페이지 비중복: {case['page_1_2_nonoverlap']}",
                f"- 동일 세션 search.ax 단독 2페이지: {case['page_2_without_mode_post']}",
                f"- 마지막 페이지 행: {case['last_page_row_count']}",
                f"- 빈 페이지({case['empty_page_number']}) 행: {case['empty_page_row_count']}",
                f"- 범위초과({case['overflow_page_number']}) 행: {case['overflow_page_row_count']}",
                "",
            ]
        )

    amendment = findings["amendment_strata"]
    lines.extend(
        [
            "## 3. GATE-AMENDMENT-STRATA",
            "",
            "| 층 | 회사 | N 체인 | Y 체인 | 판정 |",
            "|---|---|---|---|---|",
        ]
    )
    for item in amendment["strata"]:
        lines.append(
            f"| {item['name']} | {item['corp_name']} | `{', '.join(item['N_receipts'])}` | "
            f"`{', '.join(item['Y_receipts'])}` | {item['chain_passed']} |"
        )
    lines.append("")
    lines.append(
        f"아이엠증권 `20260709000043` 독립사건 assertion: {amendment['im_securities_regression_assertion']}"
    )
    for item in amendment["independent_event_samples"]:
        lines.append(
            f"- {item['name']}: `{', '.join(item['Y_receipts'])}` — 독립 보존 {item['independent_events_preserved']}"
        )

    rm = findings["rm_market"]
    lines.extend(
        [
            "",
            "## 4. GATE-RM-MARKET",
            "",
            "| 플래그 | 공식 응답의 의미 | 표본 | 신뢰도 |",
            "|---|---|---|---|",
        ]
    )
    for flag in RM_FLAGS:
        item = rm["definitions"][flag]
        receipts = [row["rcept_no"] for row in rm["samples"][flag]]
        lines.append(
            f"| `{flag}` | {item['label']} (공식 페이지 관찰: {item['label_observed_in_official_guide_response']}) | "
            f"{item['sample_count']}건 (`{', '.join(receipts) or '없음'}`) | `{item['confidence']}` |"
        )
    lines.extend(
        [
            "",
            f"관찰 조합: `{', '.join(rm['observed_rm_combinations'])}`",
            f"미확정 플래그: `{', '.join(rm['provisional_flags']) or '없음'}`",
            "",
            "## 5. GATE-D004-EQUAL-FILER",
            "",
            f"동일 제출인 목록 {findings['d004_equal_filer']['equal_filer_list_count']}건, "
            f"원문 파싱 {findings['d004_equal_filer']['document_verified_count']}건.",
            f"관찰 역할: `{', '.join(findings['d004_equal_filer']['roles_observed'])}`",
            f"대상회사 규칙 확정 가능: {findings['d004_equal_filer']['rule_can_be_finalized']}",
            "",
            "| 접수번호 | corp_name | 보고서 | 실제 역할 | 대상회사 표기 |",
            "|---|---|---|---|---|",
        ]
    )
    for item in findings["d004_equal_filer"]["documents"]:
        row = item["list_row"]
        lines.append(
            f"| `{row['rcept_no']}` | {row['corp_name']} | {row['report_nm']} | "
            f"`{item['role']}` | {item['target_label_present']} |"
        )
    lines.extend(
        [
            "",
            "## 재현성과 중단 처리",
            "",
            f"- raw: `tests/fixtures/probe/raw/{findings['run_id']}/`",
            "- golden: `tests/fixtures/probe/golden/stage0_5/`",
            f"- 요청 상한: {findings['request_limit']}",
            f"- 실행 manifest stop_reason: `{run_manifest.get('stop_reason') or 'completed_stage0_5_only'}`",
            "- 확인하지 못한 항목은 gate와 필드에서 unconfirmed/provisional로 유지했다.",
            "- 단계 1 본개발은 시작하지 않았다.",
            "",
        ]
    )
    (repo_root / "PROBE_RESULTS_STAGE0_5.md").write_text("\n".join(lines), encoding="utf-8")

    decision_marker = "<!-- STAGE0_5_DECISIONS -->"
    decisions_path = repo_root / "DECISIONS.md"
    existing = decisions_path.read_text(encoding="utf-8") if decisions_path.exists() else "# 결정 기록\n"
    if decision_marker in existing:
        existing = existing.split(decision_marker, 1)[0].rstrip() + "\n"
    decision_lines = [
        "",
        decision_marker,
        "## 단계 0.5 실측 결정",
        "",
    ]
    for gate, status in findings["gate_summary"].items():
        decision_lines.append(f"- `{gate}`: `{status}`")
    decision_lines.extend(
        [
            "- `상계납입` 42건에 비해 띄어쓴 `상계 납입`은 50,864건이고 회계상 상계 등 거짓양성이 섞였다. 기본 정밀 질의는 붙여쓴 표현을 사용하고 띄어쓰기 변형은 broad 확장에서만 사용한다.",
            "- `주금납입채무와 상계` 54건과 띄어쓴 변형 531건을 별도로 유지한다. `채권의 출자전환`은 첫 페이지에서 `출자전환` 대비 신규 접수번호 기여가 0이므로 기본 묶음에서 제외 후보로 둔다.",
            "- DART는 요청 `maxResults=100`에도 실제 10행을 반환했다. 좁은 질의는 1·2페이지가 비중복이었지만 넓은 질의는 동일 접수번호가 페이지를 넘어 반복되므로 후보예산은 행 수가 아니라 접수번호 전역 중복제거 후 계산한다.",
            "- 동일 쿠키 세션에서는 모드 설정 POST를 반복하지 않고 `search.ax`만으로 2페이지·마지막·범위초과 페이지를 조회할 수 있었다.",
            "- 유상증자·합병·전환사채 N/Y 층화 표본과 독립사건 3표본이 통과했다. Y 결과에도 같은 정규화 보고서명의 독립 사건이 함께 남으므로 접수번호 관계 없이 보고서명만으로 합치지 않는다.",
            "- `rm`의 유·코·넥·공·연은 공식 의미와 실제 표본을 함께 확보해 confirmed로 승격한다. `채`는 공식 의미만 확인되고 실제 표본이 없어 provisional로 유지한다.",
            "- D004 의견표명서는 `corp_name == flr_nm`이어도 공개매수자가 별도 회사일 수 있고 corp_name은 대상회사다. 신고서·설명서·결과보고서는 원문의 대상회사 필드를 우선하며, 자기 공개매수 여부는 공개매수자와 대상회사 필드를 비교해 판정한다.",
            "- 단계 0.5 종료 후 단계 1은 자동 시작하지 않는다.",
            "",
        ]
    )
    decisions_path.write_text(existing.rstrip() + "\n" + "\n".join(decision_lines), encoding="utf-8")

    progress = [
        "# 개발 진행 현황",
        "",
        "## 단계 0.5 실측",
        "",
        f"- run_id: `{findings['run_id']}`",
        f"- 상태: 완료 ({findings['request_count']} / {findings['request_limit']} 요청)",
    ]
    for gate, status in findings["gate_summary"].items():
        progress.append(f"- `{gate}`: `{status}`")
    progress.extend(
        [
            "- raw와 golden fixture를 분리했다.",
            "- 단계 1 본개발: 미착수",
            "- 다음 행동: 미통과·부분통과 게이트를 확인한 뒤 사용자가 별도로 개발 착수를 지시할 때까지 중단",
            "",
        ]
    )
    (repo_root / "DEVELOPMENT_PROGRESS.md").write_text("\n".join(progress), encoding="utf-8")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("stage0_5_%Y%m%dT%H%M%SZ")
