"""Offline Stage 1 fixed-query evaluation and lightweight benchmark."""

from __future__ import annotations

import json
import statistics
import time
import tracemalloc
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from app.channels.dart_fulltext import parse_search_html
from app.channels.opendart_status import classify_status, ensure_success
from app.config import defaults
from app.contracts import SearchRequest
from app.errors import SearchError
from app.orchestrator.plan_builder import build_search_plan, query_variants
from app.research.normalization import parse_report_name, parse_rm
from app.storage.session_cache import SessionTextCache

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "tests" / "golden_cases" / "stage1" / "evaluation_queries.json"


def _fixture_text(path: Path) -> str:
    payload = path.read_bytes()
    if payload.startswith(b"PK"):
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            return "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in archive.namelist())
    return payload.decode("utf-8", errors="ignore")


def evaluate_case(case: dict[str, Any], root: Path = ROOT) -> tuple[bool, list[str]]:
    errors: list[str] = []
    request = SearchRequest(**case["request"])
    fixture_text = ""
    for raw in case.get("fixtures", []):
        path = root / raw
        if not path.exists():
            errors.append(f"missing fixture: {raw}")
        else:
            fixture_text += _fixture_text(path)
    for receipt in case.get("expected_receipts", []):
        if receipt not in fixture_text:
            errors.append(f"receipt absent from fixture: {receipt}")
    if "expected_status_code" in case:
        code = case["expected_status_code"]
        status = classify_status(code)
        if code == "013" and not (status.healthy and status.no_data):
            errors.append("013 must be healthy no-data")
        elif code != "013":
            try:
                ensure_success({"status": code, "message": "fixture-evaluation"})
                errors.append(f"status {code} unexpectedly succeeded")
            except SearchError as exc:
                if exc.dart_status_code != code:
                    errors.append(f"wrong normalized status: {exc.dart_status_code}")
                if "expected_retry" in case and exc.retryable != case["expected_retry"]:
                    errors.append("retry policy mismatch")
    if "expected_rm_raw" in case:
        flags, unknown = parse_rm(case["expected_rm_raw"])
        if list(flags) != case["expected_rm_flags"]:
            errors.append("rm flags mismatch")
        if "expected_unknown_rm_flags" in case and list(unknown) != case["expected_unknown_rm_flags"]:
            errors.append("unknown rm flags mismatch")
    if "expected_report_name" in case:
        prefixes, _, _ = parse_report_name(case["expected_report_name"])
        if list(prefixes) != case["expected_prefixes"]:
            errors.append("report prefixes mismatch")
    if "expected_excluded_variants" in case:
        actual = query_variants(request.query)
        if any(item in actual for item in case["expected_excluded_variants"]):
            errors.append("broad-only variant leaked into default query")
    if "expected_search_count" in case and case.get("fixtures"):
        parsed = parse_search_html(_fixture_text(root / case["fixtures"][0]))
        if parsed.search_count != case["expected_search_count"]:
            errors.append("DART search_count mismatch")
    if case["category"] == "date_window_boundary":
        payload = json.loads(_fixture_text(root / case["fixtures"][0]))
        if payload.get("boundary_day_inclusive") is not case["expected_boundary_inclusive"]:
            errors.append("date boundary inclusion mismatch")
        if payload.get("full_unique_count") != case["expected_union_count"]:
            errors.append("date-window union mismatch")
        if payload.get("missing_from_windows") or payload.get("extra_in_windows"):
            errors.append("date-window union has gaps or extras")
    if case["category"] == "fixed_effective_page_size":
        payload = json.loads(_fixture_text(root / case["fixtures"][0]))
        if payload.get("effective_page_size") != case["expected_effective_page_size"]:
            errors.append("effective page size fixture mismatch")
    if case["category"] == "query_switch_mode_reuse":
        payload = json.loads(_fixture_text(root / case["fixtures"][0]))
        if payload.get("status") != "passed":
            errors.append("query switch fixture did not pass")
    if case["category"] == "independent_event_no_false_merge" and "independent_events_preserved" not in fixture_text:
        errors.append("independent-event preservation evidence absent")
    constant_checks = {
        "expected_effective_page_size": defaults.DART_EFFECTIVE_PAGE_SIZE,
        "expected_block_seconds": defaults.STRUCTURE_CIRCUIT_SECONDS if case["category"] == "dart_structure_circuit" else defaults.NETWORK_CIRCUIT_SECONDS,
    }
    for field, actual in constant_checks.items():
        if field in case and case[field] != actual:
            errors.append(f"constant mismatch: {field}")
    return not errors, errors


def run_evaluation(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = []
    for case in payload["queries"]:
        passed, errors = evaluate_case(case)
        results.append({"id": case["id"], "category": case["category"], "passed": passed, "errors": errors})
    return {"schema_version": payload["schema_version"], "total": len(results), "passed": sum(item["passed"] for item in results), "failed": sum(not item["passed"] for item in results), "results": results}


def benchmark(iterations: int = 1000) -> dict[str, Any]:
    request = SearchRequest("상계납입", date_from="2025-01-01", date_to="2026-01-01")
    fixture = ROOT / "tests" / "fixtures" / "probe" / "stage0_6" / "raw" / "stage0_6_20260716T163708Z" / "dart" / "query_switch" / "01_상계납입_control.html"
    html = fixture.read_text(encoding="utf-8")
    plan_times = []
    parse_times = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        build_search_plan(request)
        plan_times.append((time.perf_counter_ns() - started) / 1_000_000)
    for _ in range(max(20, iterations // 10)):
        started = time.perf_counter_ns()
        parse_search_html(html)
        parse_times.append((time.perf_counter_ns() - started) / 1_000_000)
    cache = SessionTextCache()
    tracemalloc.start()
    for index in range(defaults.SESSION_CACHE_MAX_DOCUMENTS + 5):
        cache.put(str(index), "가" * 64_000)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    percentile = lambda values: sorted(values)[max(0, int(len(values) * 0.95) - 1)]
    return {
        "plan_builder_p95_ms": round(percentile(plan_times), 4),
        "dart_parser_p95_ms": round(percentile(parse_times), 4),
        "cache_documents": len(cache),
        "cache_text_bytes": cache.total_bytes,
        "tracemalloc_current_bytes": current,
        "tracemalloc_peak_bytes": peak,
        "limits": {"cache_documents": defaults.SESSION_CACHE_MAX_DOCUMENTS, "cache_text_mb": defaults.SESSION_CACHE_MAX_TEXT_MB},
    }


def main() -> int:
    result = run_evaluation()
    result["benchmark"] = benchmark()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
