from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from app.probe.common import MASK, mask_pairs, masked_url
from app.probe.dart_web import extract_prefixes, parse_search_html
from app.probe.opendart import normalize_report_name, report_prefixes


class ProbeClassifierTests(unittest.TestCase):
    def test_explicit_zero_marker_is_normal_zero(self) -> None:
        parsed = parse_search_html(
            '<h4>검색건수 : 0</h4><table><tr><td class="no_data">조회 결과가 없습니다.</td></tr></table>'
        )
        self.assertEqual("normal_zero", parsed["classification"])
        self.assertEqual(0, parsed["result_count"])

    def test_missing_rows_and_zero_marker_is_structure_candidate(self) -> None:
        parsed = parse_search_html("<html><body><table></table></body></html>")
        self.assertEqual("structure_failure_candidate", parsed["classification"])

    def test_result_row_is_result(self) -> None:
        parsed = parse_search_html(
            "<table><tr><td>1</td><td>회사</td><td><a href=\"/dsaf001/main.do?rcpNo=20260101000001\">보고서</a></td></tr></table>"
        )
        self.assertEqual("results", parsed["classification"])
        self.assertEqual("20260101000001", parsed["rows"][0]["rcept_no"])

    def test_multiple_prefixes(self) -> None:
        report_name = "[기재정정][첨부정정]증권신고서(지분증권)"
        self.assertEqual(["기재정정", "첨부정정"], extract_prefixes(report_name))
        self.assertEqual(["기재정정", "첨부정정"], report_prefixes(report_name))
        self.assertEqual("증권신고서(지분증권)", normalize_report_name(report_name))

    def test_api_key_is_masked(self) -> None:
        self.assertEqual({"crtfc_key": MASK}, dict(mask_pairs([("crtfc_key", "secret")])))
        self.assertIn("crtfc_key=%2A%2A%2AMASKED%2A%2A%2A", masked_url("https://x.test/api?crtfc_key=secret"))
        self.assertNotIn("secret", masked_url("https://x.test/api?crtfc_key=secret"))

    def test_last_report_y_parallel_event_is_not_merged(self) -> None:
        fixture = Path(__file__).parent / "fixtures/probe/stage0_findings.json"
        findings = json.loads(fixture.read_text(encoding="utf-8"))
        case = next(
            item
            for item in findings["opendart"]["last_reprt_comparisons"]
            if item["corp_name"] == "아이엠증권"
        )
        event_receipts = set(case["Y_receipts"])
        outside_receipts = {
            row["rcept_no"] for row in case["same_name_Y_rows_outside_event"]
        }
        self.assertEqual({"20260716000411"}, event_receipts)
        self.assertIn("20260709000043", outside_receipts)
        self.assertTrue(event_receipts.isdisjoint(outside_receipts))

    def test_golden_manifest_hashes(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures/probe"
        manifest = json.loads(
            (fixture_root / "golden_manifest.json").read_text(encoding="utf-8")
        )
        for item in manifest["fixtures"]:
            body = (fixture_root / item["path"]).read_bytes()
            self.assertEqual(
                item["sha256"],
                hashlib.sha256(body).hexdigest(),
                item["path"],
            )

    def test_stage05_flagship_golden_fixture(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures/probe/golden/stage0_5"
        findings = json.loads(
            (fixture_root / "stage0_5_findings.json").read_text(encoding="utf-8")
        )
        flagship = findings["flagship_terms"]
        self.assertEqual(
            [
                "상계납입",
                "상계 납입",
                "주금납입채무와 상계",
                "주금 납입 채무와 상계",
                "출자전환",
                "채권의 출자전환",
            ],
            [item["query"] for item in flagship["queries"]],
        )
        self.assertTrue(all(item["result_count"] > 0 for item in flagship["queries"]))
        self.assertGreaterEqual(flagship["document_verified_count"], 5)
        self.assertGreaterEqual(flagship["classification_counts"]["direct_setoff_payment"], 2)
        self.assertGreaterEqual(flagship["classification_counts"]["debt_equity_conversion"], 2)
        self.assertEqual("passed", flagship["gate_status"])

    def test_stage05_pagination_observed_duplicate_is_preserved(self) -> None:
        fixture = Path(__file__).parent / "fixtures/probe/golden/stage0_5/dart_pagination.json"
        pagination = json.loads(fixture.read_text(encoding="utf-8"))
        cases = {item["label"]: item for item in pagination["cases"]}
        self.assertTrue(cases["narrow"]["page_1_2_nonoverlap"])
        self.assertFalse(cases["wide"]["page_1_2_nonoverlap"])
        self.assertTrue(cases["wide"]["page_1_2_overlap"])
        self.assertTrue(all(item["page_2_without_mode_post"] for item in cases.values()))
        self.assertEqual("partially_passed", pagination["gate_status"])

    def test_stage05_amendment_strata_and_independent_events(self) -> None:
        fixture = Path(__file__).parent / "fixtures/probe/golden/stage0_5/amendment_strata.json"
        amendment = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertTrue(all(item["chain_passed"] for item in amendment["strata"]))
        self.assertTrue(
            all(
                item["independent_events_preserved"]
                for item in amendment["independent_event_samples"]
            )
        )
        im_case = amendment["independent_event_samples"][0]
        self.assertIn("20260709000043", im_case["Y_receipts"])
        self.assertIn("20260716000411", im_case["Y_receipts"])
        self.assertTrue(amendment["im_securities_regression_assertion"])
        self.assertEqual("passed", amendment["gate_status"])

    def test_stage05_provisional_rm_flag_is_not_promoted(self) -> None:
        fixture = Path(__file__).parent / "fixtures/probe/golden/stage0_5/rm_market.json"
        rm_market = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(["채"], rm_market["provisional_flags"])
        self.assertEqual("provisional", rm_market["definitions"]["채"]["confidence"])
        self.assertEqual(0, rm_market["definitions"]["채"]["sample_count"])
        self.assertEqual("partially_passed", rm_market["gate_status"])

    def test_stage05_equal_filer_roles(self) -> None:
        fixture = Path(__file__).parent / "fixtures/probe/golden/stage0_5/d004_equal_filer.json"
        d004 = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(8, d004["equal_filer_list_count"])
        self.assertEqual(8, d004["document_verified_count"])
        self.assertTrue(d004["rule_can_be_finalized"])
        self.assertEqual("passed", d004["gate_status"])

    def test_stage05_golden_manifest_hashes(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures/probe"
        manifest = json.loads(
            (fixture_root / "golden/stage0_5/manifest.json").read_text(encoding="utf-8")
        )
        for item in manifest["files"]:
            path = fixture_root / Path(item["path"])
            self.assertEqual(item["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
