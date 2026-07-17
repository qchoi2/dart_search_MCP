from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.config.defaults import DEFAULT_SETTINGS, MIN_FIXED_EVALUATION_QUERIES, SCHEMA_VERSION
from app.evaluation import DEFAULT_CASES, benchmark, run_evaluation
from app.config import defaults
from app.mcp_server.tool_contracts import SEARCH_TOOL

ROOT = Path(__file__).resolve().parents[1]


class EvaluationTests(unittest.TestCase):
    def test_at_least_20_fixed_queries_and_required_categories(self):
        payload = json.loads(DEFAULT_CASES.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(payload["queries"]), MIN_FIXED_EVALUATION_QUERIES)
        self.assertGreaterEqual(sum(bool(item.get("fixtures")) for item in payload["queries"]), MIN_FIXED_EVALUATION_QUERIES)
        categories = {item["category"] for item in payload["queries"]}
        required = {
            "specific_company_list", "specific_company_fulltext", "precise_setoff_payment",
            "precise_obligation_setoff", "concept_debt_equity_conversion", "healthy_no_results",
            "invalid_api_key", "withdrawal_candidate", "amendment_chain", "tender_offer_target_company",
            "body_attachment_duplicate", "date_window_boundary", "independent_event_no_false_merge",
        }
        self.assertTrue(required <= categories)

    def test_all_fixed_evaluations_pass(self):
        result = run_evaluation()
        self.assertEqual(result["failed"], 0, result["results"])
        self.assertEqual(result["passed"], result["total"])

    def test_performance_and_cache_bounds(self):
        result = benchmark(iterations=100)
        self.assertLess(result["plan_builder_p95_ms"], 50)
        self.assertLess(result["dart_parser_p95_ms"], 50)
        self.assertLessEqual(result["cache_documents"], 40)
        self.assertLessEqual(result["cache_text_bytes"], 64 * 1024 * 1024)


class ConstantConsistencyTests(unittest.TestCase):
    def test_settings_match_single_source_defaults(self):
        settings = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings, DEFAULT_SETTINGS)
        self.assertFalse(settings["cache"]["ttl_disk_enabled"])

    def test_rules_and_contract_schema_versions_match(self):
        for name in ("search_terms.yaml", "amendment_rules.yaml", "ranking_rules.yaml"):
            rule = json.loads((ROOT / "app" / "rules" / name).read_text(encoding="utf-8"))
            self.assertEqual(rule["schema_version"], SCHEMA_VERSION)
        terms = json.loads((ROOT / "app" / "rules" / "search_terms.yaml").read_text(encoding="utf-8"))
        self.assertEqual(terms["precise"], ["상계납입", "주금납입채무와 상계"])
        self.assertEqual(terms["concept"], ["출자전환"])
        self.assertEqual(terms["broad_only"], ["상계 납입", "주금 납입 채무와 상계", "채권의 출자전환"])
        amendments = json.loads((ROOT / "app" / "rules" / "amendment_rules.yaml").read_text(encoding="utf-8"))
        self.assertEqual(amendments["confirmed_rm_flags"], ["유", "코", "넥", "공", "연", "채"])
        required = {"evidence_fixture", "sample_count", "sample_scope", "confidence", "checked_at"}
        for evidence in amendments["rule_evidence"].values():
            self.assertTrue(required <= evidence.keys())
            self.assertTrue((ROOT / evidence["evidence_fixture"]).exists())
            if evidence["sample_count"] < 3:
                self.assertEqual(evidence["confidence"], "provisional")

    def test_mcp_numeric_contract_uses_defaults(self):
        properties = SEARCH_TOOL["inputSchema"]["properties"]
        self.assertEqual(properties["query"]["maxLength"], defaults.QUERY_MAX_CHARS)
        self.assertEqual(properties["target_count"]["default"], defaults.DEFAULT_TARGET_COUNT)
        self.assertEqual(properties["target_count"]["maximum"], defaults.INTERACTIVE_TARGET_MAX)
        self.assertEqual(properties["max_documents"]["maximum"], defaults.DOCUMENT_BUDGET_ABSOLUTE_MAX)

    def test_plan_code_rules_and_document_constants_do_not_drift(self):
        plan = (ROOT / "DEVELOPMENT_PLAN.md").read_text(encoding="utf-8")
        required_text = (
            "effective_page_size=10", "요청 시작간격 최소 1,000ms", "40개 문서 또는 64MB",
            "15분간 open", "3분간 open", "page_count=100", '"ttl_disk_enabled": false',
        )
        for value in required_text:
            self.assertIn(value, plan)
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "app").rglob("*.py")
            if path.name != "defaults.py" and "probe" not in path.parts
        )
        self.assertNotIn("verify=False", production)
        self.assertNotIn("Mozilla/", production)
        self.assertNotIn('"maxResultsCb"', production)


if __name__ == "__main__":
    unittest.main()
