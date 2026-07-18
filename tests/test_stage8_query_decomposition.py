"""Free-text query decomposition and AND-of-OR co-occurrence verification.

Covers the fix for whole-sentence queries: search is broadened to selective
synonym terms while verification requires every concept group to co-occur, so a
sentence query is neither passed verbatim to DART nor verified as a single
verbatim substring.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from app.channels.opendart import ListCollection, candidate_from_list_row
from app.contracts import SearchRequest
from app.orchestrator.engine import SearchEngine
from app.orchestrator.plan_builder import (
    build_search_plan,
    decompose_query,
    resolve_query_terms,
)
from app.research.evidence import extract_cooccurrence_evidence

SENTENCE = "사업보고서 전직 임직원 현직 임직원 주식보상 함께 기재"


def _row(receipt: str, company: str = "테스트회사") -> dict:
    return {
        "corp_code": "001", "corp_name": company, "stock_code": "",
        "report_nm": "사업보고서", "rcept_no": receipt, "flr_nm": company,
        "rcept_dt": receipt[:8], "rm": "",
    }


class FakeOpenDart:
    def __init__(self, candidates, texts):
        self.candidates = candidates
        self.texts = texts
        self.downloads: list[str] = []

    def collect_lists(self, **kwargs):
        kwargs["diagnostics"].actual_list_requests += 1
        return ListCollection(list(self.candidates), True, None, None)

    def download_document(self, receipt_no, **kwargs):
        self.downloads.append(receipt_no)
        return self.texts.get(receipt_no, "")


class FakeDart:
    def __init__(self, candidates):
        self.candidates = candidates

    def health_check(self, diagnostics, **kwargs):
        diagnostics.health_check_requests += 1
        return True

    def search_variants(self, variants, date_from, date_to, diagnostics, **kwargs):
        diagnostics.mode_setup_requests += 1
        diagnostics.dart_result_page_requests += 1
        return list(self.candidates)


class DecompositionTests(unittest.TestCase):
    def test_sentence_splits_into_selective_variants_and_and_of_or_groups(self):
        variants, groups = decompose_query(SENTENCE)
        # Only the selective stock-compensation group is searched; broad
        # officer/tenure words are verification-only, and the report title is
        # dropped from body terms entirely.
        self.assertIn("주식매수선택권", variants)
        self.assertNotIn("임직원", variants)
        self.assertNotIn("사업보고서", variants)
        self.assertLessEqual(len(variants), 6)
        # Former-officer, current-officer, officer/employee, and stock groups.
        self.assertEqual(len(groups), 4)
        self.assertTrue(any("퇴직" in group for group in groups))
        self.assertTrue(any("재직" in group for group in groups))

    def test_setoff_and_conversion_keep_legacy_empty_groups(self):
        _, groups = resolve_query_terms("상계납입")
        self.assertEqual(groups, ())
        _, conv_groups = resolve_query_terms("채권의 출자전환")
        self.assertEqual(conv_groups, ())

    def test_plan_carries_groups_for_market_phrase_but_not_relation(self):
        market = build_search_plan(SearchRequest(SENTENCE, date_from="2026-01-01", date_to="2026-12-31"))
        self.assertEqual(market.strategy, "S3_market_rare_phrase")
        self.assertEqual(len(market.verification_term_groups), 4)
        sequence = build_search_plan(SearchRequest(
            "공개매수 후 주식교환", sequence_required=True,
            date_from="2026-01-01", date_to="2026-12-31",
        ))
        self.assertEqual(sequence.verification_term_groups, ())


class CooccurrenceEvidenceTests(unittest.TestCase):
    def setUp(self):
        _, self.groups = decompose_query(SENTENCE)

    def test_all_groups_present_verifies_with_one_snippet_per_group(self):
        text = "퇴직 임원 및 재직 임원에게 주식매수선택권을 부여한 내역"
        evidence = extract_cooccurrence_evidence("20260101000001", text, self.groups)
        self.assertTrue(evidence)
        matched = {term for item in evidence for term in item.matched_terms}
        self.assertTrue({"퇴직", "재직", "주식매수선택권"} <= matched)

    def test_missing_one_group_is_not_verified(self):
        # Stock compensation + officer present, but no former/current tenure term.
        text = "임원에게 주식매수선택권을 부여"
        self.assertEqual(extract_cooccurrence_evidence("20260101000001", text, self.groups), ())


class EngineCooccurrenceTests(unittest.TestCase):
    def _dart_candidate(self, receipt):
        base = candidate_from_list_row(_row(receipt))
        return replace(base, source_channels=("dart_fulltext",), fulltext_match_scope="body", mechanical_score=10)

    def test_engine_verifies_only_documents_where_all_concepts_cooccur(self):
        good = self._dart_candidate("20260101000001")
        false_positive = self._dart_candidate("20260101000002")
        texts = {
            good.receipt_no: "퇴직 임원과 재직 임원에 대한 주식매수선택권 부여 현황",
            false_positive.receipt_no: "주식보상비용 및 주식매수선택권 관련 재무제표 주석",
        }
        opendart = FakeOpenDart([good, false_positive], texts)
        dart = FakeDart([good, false_positive])
        result = SearchEngine(opendart=opendart, dart=dart).execute(
            SearchRequest(SENTENCE, date_from="2026-01-01", date_to="2026-12-31")
        )
        self.assertEqual(result["status"], "completed")
        verified = [item["case_id"] for item in result["results"]]
        self.assertEqual(verified, [good.receipt_no])
        excluded = {item["receipt_no"] for item in result["preliminary_candidates"]}
        self.assertIn(false_positive.receipt_no, excluded)



MNA_SENTENCE = "공개매수가 완료될 것을 전제로 한 주식매매거래의 거래종결이 이루어지도록 규정한 주식매매계약 관련 공시 사례들을 10개 정도 찾아줘"


class TokenHygieneTests(unittest.TestCase):
    def setUp(self):
        from app.orchestrator.plan_builder import search_term_rules
        self.rules = search_term_rules()

    def _clean(self, token):
        from app.orchestrator.plan_builder import _clean_token
        return _clean_token(token, self.rules)

    def test_particles_and_verb_suffixes_are_stripped(self):
        self.assertEqual(self._clean("공개매수가"), "공개매수")
        self.assertEqual(self._clean("거래종결이"), "거래종결")
        self.assertEqual(self._clean("전제로"), "전제")
        self.assertEqual(self._clean("주식매매거래의"), "주식매매거래")

    def test_directives_and_quantities_are_dropped(self):
        self.assertIsNone(self._clean("찾아줘"))
        self.assertIsNone(self._clean("이루어지도록"))
        self.assertIsNone(self._clean("10개"))

    def test_single_character_residue_is_not_stripped(self):
        # Stripping the object particle from "주가" would leave a 1-char token.
        self.assertEqual(self._clean("주가"), "주가")


class TitleConstraintTests(unittest.TestCase):
    def test_tender_offer_query_scopes_to_d004_opendart(self):
        plan = build_search_plan(SearchRequest(MNA_SENTENCE, date_from="2024-01-01", date_to="2024-12-31"))
        self.assertEqual(plan.strategy, "S3_market_rare_phrase")
        # Report(title) mode is dormant; the pool is scoped via the OpenDART
        # D004 detail type and every body concept verifies by co-occurrence.
        self.assertEqual(plan.search_mode, "contents")
        self.assertEqual(plan.primary_channel, "opendart")
        self.assertEqual(plan.secondary_channels, ("dart_fulltext",))
        self.assertEqual(plan.opendart_detail_type, "D004")
        groups = plan.verification_term_groups
        self.assertEqual(len(groups), 4)
        self.assertIn(("공개매수",), groups)
        self.assertTrue(any("주식매매계약" in group for group in groups))
        self.assertTrue(any("거래종결" in group for group in groups))
        self.assertTrue(any("전제" in group for group in groups))

    def test_search_variants_prefer_synonym_groups_first(self):
        variants, _ = decompose_query(MNA_SENTENCE)
        self.assertEqual(variants[0], "공개매수")
        self.assertIn("주식매매계약", variants)

    def test_regression_officer_stock_query_stays_contents(self):
        q2 = "사업보고서에 전직 임직원 및 현직 임직원에 대한 주식보상이 함께 기재된 사례"
        plan = build_search_plan(SearchRequest(q2, date_from="2026-01-01", date_to="2026-12-31"))
        self.assertEqual(plan.search_mode, "contents")
        self.assertNotEqual(plan.query_variants, ("공개매수",))

    def test_setoff_legacy_stays_contents_with_empty_groups(self):
        plan = build_search_plan(SearchRequest("상계납입", date_from="2026-01-01", date_to="2026-12-31"))
        self.assertEqual(plan.search_mode, "contents")
        self.assertEqual(plan.verification_term_groups, ())


class DartModePassthroughTests(unittest.TestCase):
    def test_search_variants_forwards_mode_to_search_page(self):
        from datetime import date
        from app.channels.dart_fulltext import DartFulltextClient, DartSearchPage
        from app.contracts import SearchExecutionDiagnostics
        client = DartFulltextClient()
        seen = {}

        def fake_search_page(query, date_from, date_to, diagnostics, *, mode="contents", page=1, **kwargs):
            seen["mode"] = mode
            seen["query"] = query
            return DartSearchPage("normal_zero", 0, (), ("조회 결과가 없습니다.",), 1, None, None, False)

        client.search_page = fake_search_page
        client.search_variants(["공개매수"], date(2024, 1, 1), date(2024, 12, 31), SearchExecutionDiagnostics(), mode="report")
        self.assertEqual(seen["mode"], "report")
        self.assertEqual(seen["query"], "공개매수")



class ComprehensiveShareExchangeTests(unittest.TestCase):
    Q = "공개매수 후 포괄적 주식교환이 이어진 실제 사례 10건 찾아줘"

    def test_group_included_and_directive_tokens_dropped(self):
        variants, groups = decompose_query(self.Q)
        self.assertTrue(any("포괄적 주식교환" in group for group in groups))
        self.assertIn(("공개매수",), groups)
        self.assertNotIn("이어진", variants)
        self.assertNotIn("실제", variants)
        self.assertFalse(any("10" in variant for variant in variants))

    def test_plan_scopes_to_d004(self):
        plan = build_search_plan(SearchRequest(self.Q, date_from="2023-01-01", date_to="2024-12-31"))
        self.assertEqual(plan.opendart_detail_type, "D004")
        self.assertEqual(plan.primary_channel, "opendart")


class OpenDartDetailTypeTests(unittest.TestCase):
    def _client(self):
        from app.channels.opendart import OpenDartClient
        return OpenDartClient("testkey0000000000000000000000000000000000")

    def _capture_params(self, disclosure_type):
        from datetime import date
        client = self._client()
        captured = {}

        def fake_json(endpoint, params, **kwargs):
            captured.clear()
            captured.update(params)
            return {"status": "000", "list": [], "total_count": 0, "total_page": 1}

        client._json = fake_json
        client.list_page(date_from=date(2024, 1, 1), date_to=date(2024, 12, 31), disclosure_type=disclosure_type)
        return captured

    def test_four_char_type_is_sent_as_detail_type(self):
        params = self._capture_params("D004")
        self.assertEqual(params["pblntf_detail_ty"], "D004")
        self.assertIsNone(params["pblntf_ty"])

    def test_single_char_type_is_sent_as_broad_type(self):
        params = self._capture_params("B")
        self.assertEqual(params["pblntf_ty"], "B")
        self.assertIsNone(params["pblntf_detail_ty"])


class UnscopedMarketGuardTests(unittest.TestCase):
    def test_unscoped_s3_with_dart_skips_market_listing_and_warns(self):
        # dart is present but yields nothing; the engine must not dump the whole
        # market's newest filings from OpenDART just to fill the budget.
        opendart = FakeOpenDart([candidate_from_list_row(_row("20260101000009"))], {})
        result = SearchEngine(opendart=opendart, dart=FakeDart([])).execute(
            SearchRequest("희귀한본문문구", date_from="2026-01-01", date_to="2026-01-31")
        )
        self.assertEqual(opendart.downloads, [])
        self.assertTrue(any("무필터" in warning for warning in result.get("warnings", [])))



class ScopeConfirmationTests(unittest.TestCase):
    def test_missing_period_proposes_recent_window_for_confirmation(self):
        # No period given: the engine must not scan silently; it proposes a
        # narrow default window and asks the caller to confirm.
        result = SearchEngine(opendart=None, dart=None).execute(
            SearchRequest("공개매수 거래종결 사례 찾아줘")
        )
        self.assertEqual(result["status"], "clarification_required")
        self.assertTrue(result["scope_confirmation_required"])
        scope = result["suggested_scope"]
        self.assertEqual(scope["months"], 24)
        self.assertEqual(scope["reason"], "period_unspecified")
        self.assertLess(scope["date_from"], scope["date_to"])
        self.assertIn("suggested_scope", result)

    def test_suggested_recent_scope_counts_months_back_and_clamps_month_end(self):
        from datetime import date
        from app.orchestrator.engine import _suggested_recent_scope
        self.assertEqual(_suggested_recent_scope(date(2026, 7, 18), 24), ("2024-07-18", "2026-07-18"))
        # 1 month before Mar 31 clamps to the last day of February.
        self.assertEqual(_suggested_recent_scope(date(2026, 3, 31), 1), ("2026-02-28", "2026-03-31"))



class ServerVersionTests(unittest.TestCase):
    def test_tool_result_exposes_server_version(self):
        from app.mcp_server.server import McpApplication
        from app.config.defaults import PRODUCT_VERSION
        app = McpApplication(SearchEngine(opendart=None, dart=None))
        response = app.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search_disclosure_cases", "arguments": {"query": "공개매수 거래종결 사례"}},
        })
        import json
        result = response["result"]
        payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        self.assertEqual(payload["server_version"], PRODUCT_VERSION)


if __name__ == "__main__":
    unittest.main()
