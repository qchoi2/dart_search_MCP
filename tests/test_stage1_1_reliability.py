from __future__ import annotations

import json
import hashlib
import io
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import date
from pathlib import Path

from app.channels.dart_fulltext import DartFulltextClient, parse_search_html
from app.channels.health import CircuitBreaker
from app.channels.opendart import ListCollection, OpenDartClient, candidate_from_list_row
from app.contracts import ChannelStatus, SearchExecutionDiagnostics, SearchRequest
from app.errors import ErrorCode, SearchError
from app.http_client import DeadlineBudget, HttpClient, HttpResponse
from app.orchestrator.engine import SearchEngine
from app.storage.audit_log import AuditLog

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "stage1_1"
ZERO = (FIXTURES / "dart_normal_zero.html").read_bytes()
STRUCTURE = (FIXTURES / "dart_structure_candidate.html").read_bytes()
HEALTH = (FIXTURES / "dart_health.html").read_bytes()


class FakeHttp:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []
        self.session_generation = 0

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        body = self.bodies.pop(0)
        return HttpResponse(200, {}, body, url)

    def recreate_cookie_jar(self):
        self.session_generation += 1


class EmptyOpenDart:
    def __init__(self, candidates=()):
        self.candidates = list(candidates)
        self.requests_started = 0

    def collect_lists(self, **kwargs):
        kwargs["diagnostics"].actual_list_requests += 1
        return ListCollection(list(self.candidates))

    def download_document(self, receipt_no, **kwargs):
        return ""


class EmptyDart:
    def __init__(self, *, healthy=True, error=None, breaker=None):
        self.healthy = healthy
        self.error = error
        self.breaker = breaker or CircuitBreaker()

    def health_check(self, diagnostics, **kwargs):
        diagnostics.health_check_requests += 1
        if self.error:
            raise self.error
        return self.healthy

    def search_variants(self, *args, **kwargs):
        if self.error:
            raise self.error
        return []

    def reset_session(self):
        pass


class SessionLifecycleTests(unittest.TestCase):
    def _client(self, bodies):
        http = FakeHttp(bodies)
        return DartFulltextClient(http=http, clock=lambda: 0.0, sleeper=lambda _: None), http  # type: ignore[arg-type]

    def test_new_session_first_mode_setup_once(self):
        client, http = self._client([b"mode", ZERO])
        diagnostics = SearchExecutionDiagnostics()
        client.search_page("alpha", date(2026, 1, 1), date(2026, 1, 2), diagnostics)
        self.assertEqual(diagnostics.mode_setup_requests, 1)
        self.assertEqual(len(http.requests), 2)

    def test_same_session_same_mode_keyword_switch_has_no_reset(self):
        client, _ = self._client([b"mode", ZERO, ZERO])
        diagnostics = SearchExecutionDiagnostics()
        client.search_page("alpha", date(2026, 1, 1), date(2026, 1, 2), diagnostics)
        client.search_page("beta", date(2026, 1, 1), date(2026, 1, 2), diagnostics)
        self.assertEqual(diagnostics.mode_setup_requests, 1)

    def test_mode_change_resets_once(self):
        client, _ = self._client([b"mode", ZERO, b"mode", ZERO])
        diagnostics = SearchExecutionDiagnostics()
        client.search_page("alpha", date(2026, 1, 1), date(2026, 1, 2), diagnostics, mode="contents")
        client.search_page("beta", date(2026, 1, 1), date(2026, 1, 2), diagnostics, mode="report")
        self.assertEqual(diagnostics.mode_setup_requests, 2)

    def test_reset_session_and_cookie_recreation_each_reset_once(self):
        client, http = self._client([b"mode", ZERO, b"mode", ZERO, b"mode", ZERO])
        diagnostics = SearchExecutionDiagnostics()
        client.search_page("a", date(2026, 1, 1), date(2026, 1, 2), diagnostics)
        client.reset_session()
        client.search_page("b", date(2026, 1, 1), date(2026, 1, 2), diagnostics)
        http.recreate_cookie_jar()
        client.search_page("c", date(2026, 1, 1), date(2026, 1, 2), diagnostics)
        self.assertEqual(diagnostics.mode_setup_requests, 3)


class DeadlineTests(unittest.TestCase):
    def test_http_timeout_is_minimum_of_default_and_remaining(self):
        seen = []

        class Response:
            status = 200
            headers = {}
            def read(self): return b"ok"
            def geturl(self): return "https://example.invalid"
            def __enter__(self): return self
            def __exit__(self, *args): return False

        def opener(request, **kwargs):
            seen.append(kwargs["timeout"])
            return Response()

        budget = DeadlineBudget(5.0, clock=lambda: 0.0)
        HttpClient(timeout=20, opener=opener).request("GET", "https://example.invalid", deadline=budget)
        self.assertEqual(seen, [5.0])
        self.assertTrue(budget.deadline_limited_timeout)

    def test_deadline_before_request_starts_no_network(self):
        calls = []
        client = HttpClient(opener=lambda *args, **kwargs: calls.append(1))
        with self.assertRaises(SearchError) as caught:
            client.request("GET", "https://example.invalid", deadline=DeadlineBudget(0.0, clock=lambda: 0.0))
        self.assertEqual(caught.exception.code, ErrorCode.SEARCH_TIMEOUT_PARTIAL)
        self.assertEqual(calls, [])

    def test_deadline_before_backoff_and_after_backoff(self):
        def failing(request, **kwargs):
            raise urllib.error.HTTPError(request.full_url, 500, "server", {}, None)

        sleeps = []
        with self.assertRaises(SearchError):
            HttpClient(timeout=0.1, opener=failing, sleeper=sleeps.append).request(
                "GET", "https://example.invalid", max_retries=1,
                deadline=DeadlineBudget(0.4, clock=lambda: 0.0),
            )
        self.assertEqual(sleeps, [])

        now = [0.0]
        def sleep_and_expire(seconds):
            now[0] += 1.1
        budget = DeadlineBudget(1.0, clock=lambda: now[0])
        with self.assertRaises(SearchError) as caught:
            HttpClient(timeout=0.1, opener=failing, sleeper=sleep_and_expire).request(
                "GET", "https://example.invalid", max_retries=1, deadline=budget,
            )
        self.assertEqual(caught.exception.code, ErrorCode.SEARCH_TIMEOUT_PARTIAL)
        self.assertTrue(budget.backoff_blocked)

    def test_deadline_limited_timeout_does_not_increment_dart_circuit(self):
        def timeout(request, **kwargs):
            raise TimeoutError("limited")
        http = HttpClient(timeout=20, opener=timeout)
        breaker = CircuitBreaker()
        client = DartFulltextClient(http=http, breaker=breaker, clock=lambda: 0.0, sleeper=lambda _: None)
        with self.assertRaises(SearchError) as caught:
            client.search_page(
                "x", date(2026, 1, 1), date(2026, 1, 2), SearchExecutionDiagnostics(),
                deadline=DeadlineBudget(1.0, clock=lambda: 0.0),
            )
        self.assertEqual(caught.exception.code, ErrorCode.SEARCH_TIMEOUT_PARTIAL)
        self.assertEqual(breaker.state.failure_count, 0)
        self.assertEqual(breaker.state.status, ChannelStatus.HEALTHY)

    def test_same_deadline_reaches_dart_health_mode_and_result(self):
        http = FakeHttp([HEALTH, b"mode", ZERO])
        client = DartFulltextClient(http=http, clock=lambda: 0.0, sleeper=lambda _: None)  # type: ignore[arg-type]
        diagnostics = SearchExecutionDiagnostics()
        budget = DeadlineBudget(10.0, clock=lambda: 0.0)
        self.assertTrue(client.health_check(diagnostics, deadline=budget))
        client.search_page("x", date(2026, 1, 1), date(2026, 1, 2), diagnostics, deadline=budget)
        self.assertTrue(all(kwargs["deadline"] is budget for _, _, kwargs in http.requests))

    def test_same_deadline_reaches_opendart_list_and_document(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("doc.xml", "<DOCUMENT><P>evidence</P></DOCUMENT>")

        class RecordingHttp:
            def __init__(self):
                self.deadlines = []
                self.responses = [
                    HttpResponse(200, {}, b'{"status":"013","message":"none"}', "list"),
                    HttpResponse(200, {}, stream.getvalue(), "document"),
                ]
            def request(self, method, url, **kwargs):
                self.deadlines.append(kwargs.get("deadline"))
                return self.responses.pop(0)

        http = RecordingHttp()
        client = OpenDartClient("masked", http=http)  # type: ignore[arg-type]
        budget = DeadlineBudget(10.0, clock=lambda: 0.0)
        client.list_page(date_from=date(2026, 1, 1), date_to=date(2026, 1, 2), deadline=budget)
        client.download_document("20260101000001", deadline=budget)
        self.assertEqual(http.deadlines, [budget, budget])


class StructureAndFallbackTests(unittest.TestCase):
    def test_zero_marker_outside_result_table_is_not_accepted(self):
        parsed = parse_search_html("<aside>조회 결과가 없습니다.</aside><main>unexpected</main>")
        self.assertEqual(parsed.classification, "structure_failure_candidate")

    def test_structure_diagnosis_order_and_confirmation(self):
        http = FakeHttp([b"mode", STRUCTURE, HEALTH, STRUCTURE])
        client = DartFulltextClient(http=http, clock=lambda: 0.0, sleeper=lambda _: None)  # type: ignore[arg-type]
        diagnostics = SearchExecutionDiagnostics()
        with self.assertRaises(SearchError) as caught:
            client.search_page("x", date(2026, 1, 1), date(2026, 1, 2), diagnostics)
        self.assertEqual(caught.exception.code, ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED)
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for _, url, _ in http.requests],
            ["detailSearchMain2.do", "search.ax", "main.do", "search.ax"],
        )
        self.assertEqual(diagnostics.health_check_requests, 1)
        self.assertEqual(diagnostics.structure_retry_requests, 1)

    def test_structure_diagnosis_has_reserved_requests_at_fast_budget_boundary(self):
        http = FakeHttp([b"mode", STRUCTURE, HEALTH, STRUCTURE])
        client = DartFulltextClient(http=http, clock=lambda: 0.0, sleeper=lambda _: None)  # type: ignore[arg-type]
        diagnostics = SearchExecutionDiagnostics()
        with self.assertRaises(SearchError) as caught:
            client.search_page(
                "x", date(2026, 1, 1), date(2026, 1, 2), diagnostics,
                request_budget=3,
            )
        self.assertEqual(caught.exception.code, ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED)
        self.assertEqual(diagnostics.health_check_requests, 1)
        self.assertEqual(diagnostics.structure_retry_requests, 1)

    def test_single_fallback_has_structured_warning_and_zero_block(self):
        result = SearchEngine(opendart=EmptyOpenDart(), dart=EmptyDart(healthy=False)).execute(
            SearchRequest("없는문구", date_from="2026-01-01", date_to="2026-01-02")
        )
        self.assertIn("DART_FULLTEXT_FALLBACK", result["warning_codes"])
        detail = result["warning_details"][0]
        self.assertEqual(detail["blocked_seconds"], 0)
        self.assertEqual(detail["fallback_source"], "opendart_document_search")
        self.assertEqual(result["completeness_grade"], "reduced")
        self.assertEqual(result["results"], [])

    def test_open_circuit_fallback_has_positive_block(self):
        breaker = CircuitBreaker()
        breaker.trip("structure_or_access")
        error = SearchError(
            ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN, "circuit open",
            details={"fallback_source": "opendart_document_search", "blocked_seconds": 900},
        )
        result = SearchEngine(opendart=EmptyOpenDart(), dart=EmptyDart(error=error, breaker=breaker)).execute(
            SearchRequest("없는문구", date_from="2026-01-01", date_to="2026-01-02")
        )
        self.assertGreater(result["warning_details"][0]["blocked_seconds"], 0)
        self.assertEqual(result["completeness_grade"], "reduced")

    def test_healthy_zero_without_fallback_is_not_downgraded(self):
        result = SearchEngine(opendart=EmptyOpenDart(), dart=EmptyDart()).execute(
            SearchRequest("없는문구", date_from="2026-01-01", date_to="2026-01-02")
        )
        self.assertEqual(result["warning_codes"], [])
        self.assertEqual(result["completeness_grade"], "complete")


class PrivacyAndFastPathTests(unittest.TestCase):
    def test_audit_default_removes_plain_query_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            AuditLog(path).append_summary({
                "query": "민감한 원 질의",
                "nested": {"original_query": "다른 평문"},
                "executed_query_variants": ["실행 변형"],
            })
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("query", saved)
            self.assertNotIn("original_query", saved["nested"])
            self.assertEqual(saved["executed_query_variants"], ["실행 변형"])

    def test_audit_query_text_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            AuditLog(path, audit_query_text=True).append_summary({"query": "opted in"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["query"], "opted in")

    def test_fast_path_never_merges_distinct_receipts_into_event(self):
        rows = json.loads((FIXTURES / "fast_path_distinct_receipts.json").read_text(encoding="utf-8"))
        candidates = [candidate_from_list_row(row) for row in rows]
        result = SearchEngine(opendart=EmptyOpenDart(candidates), dart=None).execute(
            SearchRequest("공시 목록", company="00123456", date_from="2026-01-01", date_to="2026-01-31", target_count=2)
        )
        self.assertEqual([item["case_id"] for item in result["results"]], [row["rcept_no"] for row in rows])
        self.assertTrue(all(item["effective_receipt_no"] is None for item in result["results"]))
        self.assertTrue(all(item["filings"][0]["event_id"] is None for item in result["results"]))

    def test_session_probe_golden_and_manifest_are_safe_and_current(self):
        golden = json.loads((ROOT / "tests" / "fixtures" / "probe" / "session_lifecycle" / "golden.json").read_text(encoding="utf-8"))
        self.assertLessEqual(golden["constraints"]["actual_requests"], golden["constraints"]["max_requests"])
        self.assertEqual(golden["constraints"]["concurrency"], 1)
        self.assertGreaterEqual(golden["constraints"]["minimum_observed_start_interval_ms"], 1000)
        self.assertFalse(golden["constraints"]["cookie_values_persisted"])
        self.assertEqual(golden["server_expiry_signal"]["status"], "unconfirmed")
        manifest = json.loads((ROOT / "session_lifecycle_manifest.json").read_text(encoding="utf-8"))
        for item in manifest["files"]:
            payload = (ROOT / item["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), item["sha256"])
            if "bytes" in item:
                self.assertEqual(len(payload), item["bytes"])


if __name__ == "__main__":
    unittest.main()
