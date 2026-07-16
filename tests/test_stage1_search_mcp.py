from __future__ import annotations

import io
import unittest
import zipfile
from dataclasses import replace
from datetime import date

from app.channels.opendart import ListCollection, candidate_from_list_row
from app.contracts import DisclosureCandidate, SearchRequest
from app.errors import ErrorCode, SearchError
from app.mcp_server.server import McpApplication
from app.orchestrator.engine import SearchEngine
from app.orchestrator.plan_builder import build_search_plan, query_variants


def document_zip(text: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("doc.xml", f"<DOCUMENT><P>{text}</P></DOCUMENT>")
    return stream.getvalue()


def row(receipt="20260101000001", company="테스트회사"):
    return {"corp_code": "001", "corp_name": company, "stock_code": "", "report_nm": "유상증자결정", "rcept_no": receipt, "flr_nm": company, "rcept_dt": receipt[:8], "rm": ""}


class FakeOpenDart:
    def __init__(self, candidates, texts=None, complete=True):
        self.candidates = candidates
        self.texts = texts or {}
        self.complete = complete
        self.downloads = []
        self.collects = 0

    def collect_lists(self, **kwargs):
        self.collects += 1
        diagnostics = kwargs["diagnostics"]
        diagnostics.actual_list_requests += 1
        return ListCollection(list(self.candidates), self.complete, 0 if not self.complete else None, 2 if not self.complete else None)

    def download_document(self, receipt_no):
        self.downloads.append(receipt_no)
        value = self.texts.get(receipt_no)
        if isinstance(value, Exception):
            raise value
        return value or ""


class FakeDart:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error
        self.calls = 0

    def health_check(self, diagnostics):
        diagnostics.health_check_requests += 1
        return True

    def search_variants(self, variants, date_from, date_to, diagnostics, **kwargs):
        self.calls += 1
        diagnostics.mode_setup_requests += 1
        diagnostics.dart_result_page_requests += 1
        if self.error:
            raise self.error
        return list(self.candidates)


class SearchExecutionTests(unittest.TestCase):
    def test_period_is_required_before_any_network(self):
        opendart = FakeOpenDart([])
        dart = FakeDart()
        result = SearchEngine(opendart=opendart, dart=dart).execute(SearchRequest("상계납입"))
        self.assertEqual(result["status"], "clarification_required")
        self.assertEqual(opendart.collects, 0)
        self.assertEqual(dart.calls, 0)

    def test_plan_is_immutable_and_broad_terms_are_not_default(self):
        request = SearchRequest("상계납입", date_from="2026-01-01", date_to="2026-01-31")
        plan = build_search_plan(request)
        self.assertEqual(plan.primary_channel, "dart_fulltext")
        self.assertNotIn("상계 납입", plan.query_variants)
        self.assertIn("주금납입채무와 상계", plan.query_variants)
        conversion = query_variants("채권의 출자전환")
        self.assertIn("채권의 출자전환", conversion)
        self.assertNotIn("상계 납입", conversion)

    def test_global_dedupe_precedes_document_budget_and_evidence_is_returned(self):
        candidate = candidate_from_list_row(row())
        dart_candidate = replace(candidate, source_channels=("dart_fulltext",), matched_terms=("상계납입",), fulltext_match_scope="body", mechanical_score=13)
        opendart = FakeOpenDart([candidate, candidate], {candidate.receipt_no: "신주의 주금납입채무와 상계하여 상계납입한다."})
        result = SearchEngine(opendart=opendart, dart=FakeDart([dart_candidate])).execute(
            SearchRequest("상계납입", date_from="2026-01-01", date_to="2026-01-31")
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(opendart.downloads, [candidate.receipt_no])
        self.assertEqual(result["diagnostics"]["health_check_requests"], 1)
        self.assertEqual(result["diagnostics"]["mode_setup_requests"], 1)
        filing = result["results"][0]["filings"][0]
        self.assertIn("dart_fulltext", filing["source_channels"])
        self.assertIn("opendart_document", filing["source_channels"])
        self.assertTrue(result["results"][0]["evidence"])

    def test_healthy_zero_is_distinct_from_channel_failure(self):
        healthy = SearchEngine(opendart=FakeOpenDart([]), dart=FakeDart([])).execute(
            SearchRequest("없는문구", date_from="2026-01-01", date_to="2026-01-02")
        )
        self.assertEqual(healthy["status"], "completed")
        self.assertEqual(healthy["results"], [])
        self.assertTrue(any("정상적으로" in warning for warning in healthy["warnings"]))
        error = SearchError(ErrorCode.DART_FULLTEXT_CIRCUIT_OPEN, "15분 차단", details={"fallback_source": "opendart_document_search"})
        failed_channel = SearchEngine(opendart=FakeOpenDart([]), dart=FakeDart(error=error)).execute(
            SearchRequest("없는문구", date_from="2026-01-01", date_to="2026-01-02")
        )
        self.assertTrue(failed_channel["coverage"]["fallback_used"])
        self.assertTrue(any("15분" in warning for warning in failed_channel["warnings"]))

    def test_partial_result_has_continuation_token(self):
        opendart = FakeOpenDart([candidate_from_list_row(row())], complete=False)
        engine = SearchEngine(opendart=opendart, dart=None)
        result = engine.execute(SearchRequest("공시 목록", company="00123456", date_from="2026-01-01", date_to="2026-01-31"))
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["continuation_token"].startswith("cursor_"))

    def test_company_name_is_resolved_before_opendart_list(self):
        seen = {}
        class CapturingOpenDart(FakeOpenDart):
            def collect_lists(self, **kwargs):
                seen["corp_code"] = kwargs.get("corp_code")
                return super().collect_lists(**kwargs)
        engine = SearchEngine(opendart=CapturingOpenDart([]), dart=None, company_resolver=lambda name: "00126380" if name == "삼성전자" else None)
        engine.execute(SearchRequest("공시 목록", company="삼성전자", date_from="2026-01-01", date_to="2026-01-31"))
        self.assertEqual(seen["corp_code"], "00126380")

    def test_exhaustive_does_not_start_batch(self):
        opendart = FakeOpenDart([])
        result = SearchEngine(opendart=opendart, dart=None).execute(
            SearchRequest("전수", date_from="2026-01-01", date_to="2026-01-31", exhaustive=True)
        )
        self.assertEqual(result["status"], "batch_confirmation_required")
        self.assertEqual(opendart.collects, 0)

    def test_opendart_auth_status_becomes_api_key_action(self):
        class AuthFailure(FakeOpenDart):
            def collect_lists(self, **kwargs):
                raise SearchError(ErrorCode.OPENDART_KEY_UNREGISTERED, "등록되지 않은 키", dart_status_code="010")
        result = SearchEngine(opendart=AuthFailure([]), dart=None).execute(
            SearchRequest("공시 목록", company="00123456", date_from="2026-01-01", date_to="2026-01-31")
        )
        self.assertEqual(result["status"], "api_key_action_required")
        self.assertEqual(result["error"]["dart_status_code"], "010")

    def test_dart_candidates_without_api_key_are_not_marked_complete(self):
        candidate = replace(candidate_from_list_row(row()), source_channels=("dart_fulltext",), matched_terms=("상계납입",))
        result = SearchEngine(opendart=None, dart=FakeDart([candidate])).execute(
            SearchRequest("상계납입", date_from="2026-01-01", date_to="2026-01-31")
        )
        self.assertEqual(result["status"], "api_key_action_required")
        self.assertEqual(result["results"], [])

    def test_evidence_tool_limits_and_never_returns_full_document(self):
        receipt = "20260101000001"
        opendart = FakeOpenDart([], {receipt: ("상계납입 근거 " * 1000)})
        result = SearchEngine(opendart=opendart, dart=None).get_evidence(receipt, ["상계납입"], include_full_preview=True)
        self.assertLessEqual(len(result["evidence"]), 8)
        self.assertTrue(all(len(item["text"]) <= 500 for item in result["evidence"]))
        self.assertFalse(result["include_full_preview"])


class McpTests(unittest.TestCase):
    def test_tool_listing_and_call(self):
        app = McpApplication(SearchEngine(opendart=FakeOpenDart([]), dart=FakeDart([])))
        listing = app.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [item["name"] for item in listing["result"]["tools"]]
        self.assertEqual(names, ["search_disclosure_cases", "get_disclosure_evidence"])
        called = app.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_disclosure_cases", "arguments": {"query": "x"}}})
        self.assertEqual(called["result"]["structuredContent"]["status"], "clarification_required")


if __name__ == "__main__":
    unittest.main()
