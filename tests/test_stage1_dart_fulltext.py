from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from app.channels.dart_fulltext import (
    DartFulltextClient,
    DartResultRow,
    dart_date_windows,
    merge_duplicate_rows,
    parse_search_html,
    row_to_candidate,
)
from app.channels.health import CircuitBreaker
from app.config.defaults import DART_EFFECTIVE_PAGE_SIZE, NETWORK_CIRCUIT_SECONDS, USER_AGENT
from app.contracts import ChannelStatus, SearchExecutionDiagnostics
from app.errors import ErrorCode, SearchError
from app.http_client import HttpResponse

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "probe" / "stage0_6" / "raw" / "stage0_6_20260716T163708Z" / "dart"


class FakeHttp:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        body = self.bodies.pop(0)
        if isinstance(body, str):
            body = body.encode()
        return HttpResponse(200, {}, body, url)


class FulltextTests(unittest.TestCase):
    def test_real_fixture_parses_structured_rows_and_count(self):
        text = (FIXTURE / "query_switch" / "01_상계납입_control.html").read_text(encoding="utf-8")
        result = parse_search_html(text)
        self.assertEqual(result.classification, "results")
        self.assertEqual(result.search_count, 42)
        self.assertEqual(len(result.rows), DART_EFFECTIVE_PAGE_SIZE)
        first = result.rows[0]
        self.assertEqual(first.receipt_no, "20260708000160")
        self.assertEqual(first.market, "유")
        self.assertEqual(first.disclosure_group, "공정위공시")
        self.assertEqual(first.match_scope, "body")
        self.assertEqual(first.filer_name, "영풍")
        self.assertEqual(first.receipt_date, "20260708")

    def test_mode_setup_is_not_repeated_for_keyword_switch(self):
        html_a = (FIXTURE / "query_switch" / "01_상계납입_control.html").read_bytes()
        html_b = (FIXTURE / "query_switch" / "02_주금납입채무와_상계_direct.html").read_bytes()
        http = FakeHttp([b"mode", html_a, html_b])
        times = iter([0.0, 0.0, 1.0, 1.0, 2.0, 2.0])
        client = DartFulltextClient(http=http, clock=lambda: next(times), sleeper=lambda _: None)  # type: ignore[arg-type]
        diagnostics = SearchExecutionDiagnostics()
        client.search_page("상계납입", date(2025, 1, 1), date(2026, 1, 1), diagnostics)
        client.search_page("주금납입채무와 상계", date(2025, 1, 1), date(2026, 1, 1), diagnostics)
        self.assertEqual(diagnostics.mode_setup_requests, 1)
        self.assertEqual(diagnostics.dart_result_page_requests, 2)
        self.assertEqual([url.rsplit("/", 1)[-1] for _, url, _ in http.requests], ["detailSearchMain2.do", "search.ax", "search.ax"])

    def test_form_fixed_page_size_and_inclusive_dates(self):
        form = DartFulltextClient._form("상계납입", date(2026, 1, 2), date(2026, 2, 3), "contents", 1)
        self.assertEqual(form["maxResults"], "10")
        self.assertNotIn("maxResultsCb", form)
        self.assertEqual(form["startDate"], "20260102")
        self.assertEqual(form["endDate"], "20260203")

    def test_form_uses_resolved_company_code_for_dart_filter(self):
        form = DartFulltextClient._form("주요사항보고서", date(2025, 1, 1), date(2025, 12, 31), "contents", 1, "00126380")
        self.assertEqual(form["textCrpCik"], "00126380")
        self.assertEqual(form["b_textCrpCik"], "00126380")
        self.assertEqual(form["textCrpNm"], "")

    def test_nonoverlap_date_windows_cover_entire_period(self):
        windows = dart_date_windows(date(2026, 1, 1), date(2026, 1, 10), 3)
        self.assertEqual(windows[0], (date(2026, 1, 1), date(2026, 1, 3)))
        self.assertEqual(windows[-1], (date(2026, 1, 10), date(2026, 1, 10)))
        for left, right in zip(windows, windows[1:]):
            self.assertEqual(left[1].toordinal() + 1, right[0].toordinal())

    def test_body_attachment_dedupe_prefers_body_and_preserves_tags(self):
        common = dict(receipt_no="20260101000001", corp_code=None, company="회사", market="유", report_name="보고서", report_name_prefixes=(), snippet="text", disclosure_group="발행공시", filer_name="회사", receipt_date="20260101")
        attachment = DartResultRow(**common, match_scope="attachment", row_tags=("발행공시", "첨부문서"))
        body = DartResultRow(**common, match_scope="body", row_tags=("발행공시", "본문"))
        merged = merge_duplicate_rows([attachment, body])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].match_scope, "mixed")
        self.assertIn("본문", merged[0].row_tags)

    def test_normal_zero_stays_healthy(self):
        parsed = parse_search_html('<div id="result"><h4 id="searchCnt">검색건수 : 0</h4><p>조회 결과가 없습니다.</p></div>')
        self.assertEqual(parsed.classification, "normal_zero")

    def test_structure_failure_requires_retry_then_opens_15_minute_circuit(self):
        clock = [1000.0]
        breaker = CircuitBreaker(clock=lambda: clock[0])
        http = FakeHttp([b"mode", b"<html>changed</html>", b"<html>changed again</html>"])
        client = DartFulltextClient(http=http, breaker=breaker, clock=lambda: clock[0], sleeper=lambda _: None)  # type: ignore[arg-type]
        diagnostics = SearchExecutionDiagnostics()
        with self.assertRaises(SearchError) as caught:
            client.search_page("x", date(2026, 1, 1), date(2026, 1, 2), diagnostics)
        self.assertEqual(caught.exception.code, ErrorCode.DART_FULLTEXT_STRUCTURE_CHANGED)
        self.assertEqual(diagnostics.health_check_requests, 1)
        self.assertEqual(diagnostics.dart_result_page_requests, 1)
        self.assertEqual(breaker.state.status, ChannelStatus.CIRCUIT_OPEN)
        self.assertEqual(breaker.state.blocked_until, 1900.0)
        request_count = len(http.requests)
        with self.assertRaises(SearchError) as second:
            client.search_page("y", date(2026, 1, 1), date(2026, 1, 2), diagnostics)
        self.assertEqual(second.exception.code, ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN)
        self.assertEqual(len(http.requests), request_count)

    def test_identifying_user_agent_is_not_browser(self):
        self.assertIn("dart-search-mcp", USER_AGENT)
        self.assertNotIn("Mozilla", USER_AGENT)

    def test_request_start_interval_is_at_least_one_second(self):
        current = [0.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            current[0] += seconds
        client = DartFulltextClient(http=FakeHttp([b"a", b"b"]), clock=lambda: current[0], sleeper=sleep)  # type: ignore[arg-type]
        client._paced_request("GET", "https://example.invalid/one")
        client._paced_request("GET", "https://example.invalid/two")
        self.assertEqual(sleeps, [1.0])

    def test_network_failure_circuit_is_three_minutes(self):
        clock = [100.0]
        breaker = CircuitBreaker(clock=lambda: clock[0])
        self.assertEqual(breaker.failure("network"), ChannelStatus.DEGRADED)
        self.assertEqual(breaker.failure("network"), ChannelStatus.CIRCUIT_OPEN)
        self.assertEqual(breaker.state.blocked_until, 100.0 + NETWORK_CIRCUIT_SECONDS)

    def test_exhaustive_date_window_primitive_dedupes_global_receipts(self):
        base = DartResultRow(
            receipt_no="20260101000001", corp_code=None, company="회사", market="유",
            report_name="보고서", report_name_prefixes=(), snippet="상계납입", disclosure_group="발행공시",
            match_scope="body", filer_name="회사", receipt_date="20260101", row_tags=("본문",),
        )
        candidate = row_to_candidate(base, "상계납입")
        class WindowClient(DartFulltextClient):
            def __init__(self):
                pass
            def search_variants(self, queries, date_from, date_to, diagnostics, **kwargs):
                diagnostics.dart_result_page_requests += 1
                return [candidate]
        diagnostics = SearchExecutionDiagnostics()
        result = WindowClient().search_date_windows(
            ["상계납입"], date(2026, 1, 1), date(2026, 1, 6), diagnostics,
            window_days=3, request_budget=10,
        )
        self.assertTrue(result.complete)
        self.assertTrue(result.continuous)
        self.assertEqual(len(result.windows), 2)
        self.assertEqual(len(result.candidates), 1)


if __name__ == "__main__":
    unittest.main()
