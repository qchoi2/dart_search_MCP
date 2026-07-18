from __future__ import annotations

import unittest

from app.channels.opendart import ListCollection, candidate_from_list_row
from app.config import defaults
from app.contracts import SearchRequest
from app.errors import ErrorCode, SearchError
from app.mcp_server.server import McpApplication
from app.mcp_server.tool_contracts import EVIDENCE_TOOL, SEARCH_TOOL
from app.orchestrator.engine import SearchEngine
from app.orchestrator.plan_builder import build_search_plan
from app.storage.continuation import ContinuationStore


def _row(index: int = 1) -> dict[str, str]:
    receipt = f"20260101{index:06d}"
    return {
        "corp_code": "00123456",
        "corp_name": "테스트회사",
        "stock_code": "",
        "report_nm": "주요사항보고서",
        "rcept_no": receipt,
        "flr_nm": "테스트회사",
        "rcept_dt": receipt[:8],
        "rm": "",
    }


class FakeOpenDart:
    def __init__(self, candidates=(), *, complete=True, text="검색어 근거"):
        self.candidates = list(candidates)
        self.complete = complete
        self.text = text
        self.collects = 0
        self.requests_started = 0

    def collect_lists(self, **kwargs):
        self.collects += 1
        kwargs["diagnostics"].actual_list_requests += 1
        return ListCollection(
            list(self.candidates),
            complete=self.complete,
            next_window_index=None if self.complete else 1,
            next_page=None if self.complete else 2,
        )

    def download_document(self, receipt_no, **kwargs):
        self.requests_started += 1
        return self.text


class NoNetworkDart:
    def __init__(self, *, latest_first=False):
        self.latest_first = latest_first
        self.calls = 0

    def health_check(self, diagnostics, **kwargs):
        diagnostics.health_check_requests += 1
        return True

    def search_variants(self, variants, date_from, date_to, diagnostics, **kwargs):
        self.calls += 1
        if self.latest_first:
            diagnostics.latest_first_bias = True
            diagnostics.fully_pageable_by_query[variants[0]] = False
        return []


class Stage3ContractTests(unittest.TestCase):
    def test_tool_schema_exposes_the_complete_interactive_search_request(self):
        properties = SEARCH_TOOL["inputSchema"]["properties"]
        self.assertTrue({
            "query", "company", "date_from", "date_to", "target_count", "mode",
            "max_documents", "cache_mode", "exhaustive", "amendment_comparison",
            "sequence_required", "output_mode", "continuation_token", "schema_version",
        } <= properties.keys())
        self.assertEqual(properties["output_mode"]["enum"], ["interactive"])
        self.assertEqual(properties["schema_version"]["const"], defaults.SCHEMA_VERSION)
        keyword_schema = EVIDENCE_TOOL["inputSchema"]["properties"]["keywords"]
        self.assertEqual(keyword_schema["maxItems"], defaults.INTERACTIVE_TARGET_MAX)

    def test_search_request_rejects_boundary_type_and_inactive_cache_values(self):
        invalid = (
            {"target_count": True},
            {"max_documents": True},
            {"cache_mode": "temporary_ttl"},
            {"output_mode": "unknown"},
            {"amendment_comparison": "yes"},
            {"schema_version": "2.0"},
            {"date_from": "2026-99-99"},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SearchRequest("x", **kwargs)

    def test_interactive_document_and_result_caps_follow_defaults(self):
        plan = build_search_plan(SearchRequest(
            "목록", company="00123456", target_count=20,
            max_documents=defaults.DOCUMENT_BUDGET_ABSOLUTE_MAX,
            date_from="2026-01-01", date_to="2026-01-31",
        ))
        self.assertEqual(plan.effective_document_budget, defaults.STANDARD_DOCUMENT_BUDGET)
        self.assertEqual(plan.result_budget, defaults.INTERACTIVE_TARGET_MAX)


class Stage3ExecutionTests(unittest.TestCase):
    def test_batch_does_not_start_and_stage6_relation_hint_routes_on_demand(self):
        opendart = FakeOpenDart()
        dart = NoNetworkDart()
        engine = SearchEngine(opendart=opendart, dart=dart)
        batch = engine.execute(SearchRequest(
            "전수", output_mode="batch", target_count=100,
            date_from="2026-01-01", date_to="2026-01-31",
        ))
        self.assertEqual(batch["status"], "batch_confirmation_required")
        self.assertFalse(batch["coverage"]["complete"])
        self.assertEqual(batch["completeness_grade"], "unconfirmed")
        relation = engine.execute(SearchRequest(
            "정정 비교", amendment_comparison=True,
            date_from="2026-01-01", date_to="2026-01-31",
        ))
        self.assertEqual(relation["status"], "completed")
        self.assertEqual(relation["plan"]["strategy"], "S6_amendment_comparison")
        self.assertEqual(relation["relation_analysis"]["strategy"], "S6_amendment_comparison")
        self.assertEqual(opendart.collects, 1)
        self.assertEqual(dart.calls, 1)

    def test_continuation_records_period_variants_and_budget_warning(self):
        store = ContinuationStore()
        candidate = candidate_from_list_row(_row())
        engine = SearchEngine(
            opendart=FakeOpenDart([candidate], complete=False),
            dart=None,
            continuations=store,
        )
        result = engine.execute(SearchRequest(
            "공시 목록", company="00123456",
            date_from="2026-01-01", date_to="2026-01-31",
        ))
        self.assertEqual(result["status"], "partial")
        self.assertIn("SEARCH_BUDGET_PARTIAL", result["warning_codes"])
        state = store.consume(result["continuation_token"])
        self.assertEqual(state["date_from"], "2026-01-01")
        self.assertEqual(state["date_to"], "2026-01-31")
        self.assertEqual(tuple(state["query_variants"]), tuple(result["plan"]["query_variants"]))

    def test_wrong_lineage_does_not_destroy_continuation(self):
        store = ContinuationStore()
        first = SearchEngine(
            opendart=FakeOpenDart([candidate_from_list_row(_row())], complete=False),
            dart=None,
            continuations=store,
        ).execute(SearchRequest(
            "공시 목록", company="00123456",
            date_from="2026-01-01", date_to="2026-01-31",
        ))
        token = first["continuation_token"]
        engine = SearchEngine(opendart=FakeOpenDart(), dart=None, continuations=store)
        with self.assertRaises(SearchError) as caught:
            engine.execute(SearchRequest(
                "다른 검색", company="00123456", continuation_token=token,
                date_from="2026-01-01", date_to="2026-01-31",
            ))
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_CONTINUATION_TOKEN)
        self.assertEqual(store.consume(token)["lineage"], first["search_lineage_id"])

    def test_latest_first_bias_has_structured_warning(self):
        result = SearchEngine(
            opendart=FakeOpenDart(),
            dart=NoNetworkDart(latest_first=True),
        ).execute(SearchRequest(
            "희귀문구", date_from="2026-01-01", date_to="2026-01-31",
        ))
        self.assertIn("LATEST_FIRST_BIAS", result["warning_codes"])
        detail = next(item for item in result["warning_details"] if item["code"] == "LATEST_FIRST_BIAS")
        self.assertIn(False, detail["fully_pageable_by_query"].values())
        self.assertEqual(result["completeness_grade"], "reduced")

    def test_result_limit_and_receipt_scoped_case_units(self):
        candidates = [candidate_from_list_row(_row(index)) for index in range(1, 26)]
        result = SearchEngine(opendart=FakeOpenDart(candidates), dart=None).execute(SearchRequest(
            "공시 목록", company="00123456", target_count=20,
            date_from="2026-01-01", date_to="2026-01-31",
        ))
        self.assertEqual(len(result["results"]), 20)
        self.assertEqual(len({item["case_id"] for item in result["results"]}), 20)
        self.assertTrue(all(len(item["filings"]) == 1 for item in result["results"]))
        self.assertTrue(all(item["mechanical_findings"] for item in result["results"]))
        self.assertTrue(all(item["legal_assessment"] is None for item in result["results"]))
        self.assertTrue(result["source_link_policy"]["required_for_every_presented_result"])
        self.assertTrue(all(item["original_document_url"].startswith("https://dart.fss.or.kr/") for item in result["results"]))
        self.assertTrue(all(item["original_document_links"] for item in result["results"]))
        self.assertTrue(all(item["original_document_markdown"].startswith("[DART 공시 원문 보기](https://") for item in result["results"]))
        self.assertTrue(all(
            link["url"].endswith(link["receipt_no"])
            for item in result["results"]
            for link in item["original_document_links"]
        ))

    def test_evidence_response_is_bounded_structured_and_untrusted(self):
        receipt = "20260101000001"
        text = ("검색어 근거 문장 " + "x" * 600 + " ") * 12
        engine = SearchEngine(opendart=FakeOpenDart(text=text), dart=None)
        result = engine.get_evidence(receipt, ["검색어"], include_full_preview=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["schema_version"], defaults.SCHEMA_VERSION)
        self.assertTrue(result["source_text_untrusted"])
        self.assertLessEqual(result["evidence_count"], defaults.EVIDENCE_MAX_SNIPPETS)
        self.assertTrue(all(len(item["text"]) <= defaults.EVIDENCE_MAX_CHARS for item in result["evidence"]))
        self.assertTrue(all("content" not in item["content_boundary"] for item in result["evidence"]))
        self.assertFalse(result["include_full_preview"])
        self.assertTrue(result["full_preview_ignored"])
        self.assertIsInstance(result["amendment_context"], dict)
        for keywords in ([], ["x", "x"], ["x"] * (defaults.INTERACTIVE_TARGET_MAX + 1)):
            with self.subTest(keywords=keywords), self.assertRaises(ValueError):
                engine.get_evidence(receipt, keywords)


class ContinuationStoreTests(unittest.TestCase):
    def test_ttl_and_entry_bound_are_deterministic(self):
        now = [100.0]
        store = ContinuationStore(ttl_seconds=10, max_entries=2, clock=lambda: now[0])
        oldest = store.issue({"value": 1})
        now[0] += 1
        middle = store.issue({"value": 2})
        now[0] += 1
        newest = store.issue({"value": 3})
        with self.assertRaises(SearchError):
            store.consume(oldest)
        self.assertEqual(store.consume(middle)["value"], 2)
        self.assertEqual(store.consume(newest)["value"], 3)
        now[0] += 11
        with self.assertRaises(SearchError):
            store.consume(newest)


class Stage3McpBoundaryTests(unittest.TestCase):
    def test_invalid_evidence_arguments_return_structured_mcp_error(self):
        app = McpApplication(SearchEngine(opendart=FakeOpenDart(), dart=None))
        response = app.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_disclosure_evidence",
                "arguments": {"receipt_no": "bad", "keywords": []},
            },
        })
        payload = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
