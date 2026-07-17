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


if __name__ == "__main__":
    unittest.main()
