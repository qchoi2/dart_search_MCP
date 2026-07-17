from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from pathlib import Path

from app.channels.opendart import candidate_from_list_row
from app.contracts import VerifiedCase
from app.contracts import SearchRequest
from app.orchestrator.engine import SearchEngine
from app.orchestrator.plan_builder import build_search_plan
from app.research.amendments import (
    AmendmentContext,
    build_amendment_chains,
    compare_amendment_chain,
    extract_amendment_context,
)
from app.research.events import build_event_graph, extract_event_parties
from app.research.structural_diff import classify_change, compare_structured_fields


ROOT = Path(__file__).resolve().parents[1]


def _row(receipt: str, *, company: str = "테스트회사", report: str = "주요사항보고서(유상증자결정)", prefix: bool = False) -> dict:
    return {
        "corp_code": "00123456",
        "corp_name": company,
        "stock_code": "",
        "report_nm": ("[기재정정]" if prefix else "") + report,
        "rcept_no": receipt,
        "flr_nm": company,
        "rcept_dt": receipt[:8],
        "rm": "",
    }


class AmendmentParserTests(unittest.TestCase):
    def test_on_demand_plans_use_relation_terms_and_amendment_budget(self):
        amendment = build_search_plan(SearchRequest(
            "납입일이 뒤로 정정된 사례", amendment_comparison=True,
            date_from="2026-01-01", date_to="2026-12-31",
        ))
        self.assertEqual(amendment.strategy, "S6_amendment_comparison")
        self.assertEqual(amendment.query_variants, ("납입일",))
        self.assertEqual(amendment.effective_document_budget, 80)
        sequence = build_search_plan(SearchRequest(
            "공개매수 후 주식교환", sequence_required=True,
            date_from="2026-01-01", date_to="2026-12-31",
        ))
        self.assertEqual(sequence.strategy, "S5_event_sequence")
        self.assertEqual(sequence.query_variants, ("공개매수", "주식교환"))

    def test_correction_table_is_preferred_and_direction_is_structured(self):
        text = (
            "공시서류의 최초제출일 : 2026년 05월 08일\n"
            "3. 정정사항\n"
            "납입일 일정 변경 정 정 전: 2026년 7월 10일 정 정 후: 2026년 7월 20일;\n"
            "전환가액 조건 변경 정 정 전: 1,200원 정 정 후: 1,000원;"
        )
        context = extract_amendment_context(text, receipt_no="20260716000809")
        self.assertEqual(context.original_filing_date, "20260508")
        self.assertTrue(context.has_correction_table)
        self.assertEqual([row.direction for row in context.correction_rows], ["postponed", "decreased"])

    def test_field_diff_classifies_dates_numbers_and_text(self):
        self.assertEqual(classify_change("2026.07.10", "2026.07.20"), "postponed")
        self.assertEqual(classify_change("1,200원", "1,000원"), "decreased")
        changes = compare_structured_fields("납입일: 2026.07.10 전환가액: 1,200원", "납입일: 2026.07.20 전환가액: 1,000원")
        self.assertEqual({item["direction"] for item in changes}, {"postponed", "decreased"})


class AmendmentChainAccuracyTests(unittest.TestCase):
    def test_stage05_twenty_receipt_ground_truth_is_100_percent(self):
        fixture = json.loads((ROOT / "tests/fixtures/probe/golden/stage0_5/amendment_strata.json").read_text(encoding="utf-8"))
        candidates = []
        contexts: dict[str, AmendmentContext] = {}
        expected_group: dict[str, str | None] = {}
        for stratum in fixture["strata"]:
            original = stratum["original_receipt"]
            for row in stratum["N_chain_rows"]:
                candidate = candidate_from_list_row(row)
                candidates.append(candidate)
                expected_group[candidate.receipt_no] = original
                contexts[candidate.receipt_no] = AmendmentContext(
                    candidate.receipt_no,
                    None if candidate.receipt_no == original else original,
                    None,
                    None if candidate.receipt_no == original else "explicit_original_receipt_no",
                    (),
                    candidate.receipt_no != original,
                )
        for sample in fixture["independent_event_samples"]:
            for row in sample["Y_rows"]:
                candidate = candidate_from_list_row(row)
                candidates.append(candidate)
                expected_group[candidate.receipt_no] = None
                contexts[candidate.receipt_no] = AmendmentContext(candidate.receipt_no, None, None, None, (), True)
        chains = build_amendment_chains(candidates, contexts)
        predicted: dict[str, str | None] = {receipt: None for receipt in expected_group}
        for chain in chains:
            if len(chain["member_receipt_nos"]) > 1 and chain["chain_confidence"] == "confirmed":
                for receipt in chain["member_receipt_nos"]:
                    predicted[receipt] = chain["original_receipt_no"]
        correct = sum(predicted[key] == value for key, value in expected_group.items())
        accuracy = correct / len(expected_group)
        self.assertEqual(len(expected_group), 20)
        self.assertGreaterEqual(accuracy, 0.95)
        self.assertEqual(accuracy, 1.0)
        self.assertIsNone(predicted["20260709000043"])

    def test_unlinked_same_company_title_date_is_never_confirmed(self):
        candidates = [
            candidate_from_list_row(_row("20260716000001", prefix=True)),
            candidate_from_list_row(_row("20260716000002", prefix=True)),
        ]
        contexts = {item.receipt_no: AmendmentContext(item.receipt_no, None, None, None, (), True) for item in candidates}
        chains = build_amendment_chains(candidates, contexts)
        self.assertTrue(all(chain["chain_confidence"] == "uncertain" for chain in chains))
        self.assertTrue(all(len(chain["member_receipt_nos"]) == 1 for chain in chains))

    def test_engine_groups_linked_filings_so_amendments_do_not_consume_case_count(self):
        original = candidate_from_list_row(_row("20260508000001"))
        final = candidate_from_list_row(_row("20260716000001", prefix=True))
        engine = SearchEngine(opendart=None, dart=None)
        engine.cache.put(original.receipt_no, "납입일: 2026.07.10")
        engine.cache.put(final.receipt_no, "원접수번호 20260508000001\n납입일 정 정 전: 2026.07.10 정 정 후: 2026.07.20;")
        cases = [engine._to_case(original, "정정"), engine._to_case(final, "정정")]
        analysis, grouped = engine._relation_analysis("S6_amendment_comparison", cases)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(len(grouped[0].filings), 2)
        self.assertEqual(analysis["confirmed_chain_count"], 1)
        comparison = analysis["comparisons"][0]
        self.assertTrue(comparison["comparison_complete"])
        self.assertEqual(comparison["changes"][0]["source"], "correction_table")


class EventGraphTests(unittest.TestCase):
    def test_party_value_stops_before_the_next_relation_field(self):
        parties = extract_event_parties(
            "공개매수자: 인수자 주식회사 공개매수 대상회사명: 대상회사 주식회사"
        )
        self.assertEqual(parties["offeror"], "인수자")
        self.assertEqual(parties["target_company"], "대상회사")

    def test_tender_offer_to_share_exchange_requires_explicit_shared_party(self):
        tender = candidate_from_list_row(_row("20260101000001", company="인수자", report="공개매수신고서"))
        exchange = candidate_from_list_row(_row("20260201000001", company="인수자", report="주요사항보고서(주식교환ㆍ이전결정)"))
        texts = {
            tender.receipt_no: "공개매수자: 인수자 주식회사 공개매수 대상회사명: 대상회사 주식회사",
            exchange.receipt_no: "주식교환 상대회사: 대상회사 주식회사",
        }
        graph = build_event_graph([tender, exchange], texts)
        self.assertEqual(graph["confirmed_edge_count"], 1)
        self.assertEqual(graph["edges"][0]["relation"], "tender_offer_precedes_share_exchange")
        self.assertTrue(graph["edges"][0]["confirmed"])

    def test_company_name_only_sequence_stays_uncertain(self):
        tender = candidate_from_list_row(_row("20260101000001", report="공개매수신고서"))
        exchange = candidate_from_list_row(_row("20260201000001", report="주요사항보고서(주식교환ㆍ이전결정)"))
        graph = build_event_graph([tender, exchange], {})
        self.assertEqual(graph["confirmed_edge_count"], 0)
        self.assertEqual(graph["uncertain_edge_count"], 1)
        self.assertFalse(graph["edges"][0]["confirmed"])


if __name__ == "__main__":
    unittest.main()
