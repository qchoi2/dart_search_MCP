from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.channels.opendart import (
    CompanyDirectory,
    OpenDartClient,
    batch_companies,
    candidate_from_list_row,
    normalize_document_zip,
    split_date_windows,
)
from app.channels.opendart_status import classify_status, ensure_success
from app.contracts import SearchExecutionDiagnostics
from app.errors import ErrorCode, SearchError
from app.http_client import HttpResponse
from app.research.evidence import extract_evidence
from app.research.normalization import dart_viewer_url, parse_report_name, parse_rm

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "probe"


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, bytes):
            return HttpResponse(200, {}, value, url)
        return HttpResponse(200, {}, json.dumps(value, ensure_ascii=False).encode(), url)


class OpenDartTests(unittest.TestCase):
    def test_date_windows_are_inclusive_contiguous_and_three_months(self):
        windows = split_date_windows(date(2025, 1, 31), date(2026, 1, 31))
        self.assertEqual(windows[0].date_from, date(2025, 1, 31))
        self.assertEqual(windows[-1].date_to, date(2026, 1, 31))
        for left, right in zip(windows, windows[1:]):
            self.assertEqual(left.date_to.toordinal() + 1, right.date_from.toordinal())

    def test_company_batches_are_deduped_and_at_most_100(self):
        batches = batch_companies([f"{n:08d}" for n in range(205)] + ["00000001"])
        self.assertEqual([len(x) for x in batches], [100, 100, 5])

    def test_company_directory_parses_real_probe_fixture(self):
        payload = (FIXTURES / "opendart" / "corp_codes" / "corpCode.zip").read_bytes()
        directory = CompanyDirectory.from_zip(payload)
        self.assertGreater(len(directory.records), 100000)
        self.assertEqual(directory.lookup("삼성전자")[0].stock_code, "005930")
        self.assertEqual(directory.lookup("005930")[0].corp_name, "삼성전자")

    def test_status_contract(self):
        self.assertTrue(classify_status("000").healthy)
        self.assertTrue(classify_status("013").healthy)
        self.assertTrue(classify_status("013").no_data)
        for code in ("010", "011", "012", "014", "020", "021", "100", "101", "800", "901"):
            with self.subTest(code=code), self.assertRaises(SearchError) as caught:
                ensure_success({"status": code, "message": "x"})
            self.assertEqual(caught.exception.dart_status_code, code)
            self.assertFalse(caught.exception.retryable)
        self.assertTrue(classify_status("900").retryable)

    def test_list_pagination_parameters_and_global_dedupe(self):
        page1 = {"status": "000", "total_count": 2, "total_page": 2, "list": [self._row("20260102000001"), self._row("20260102000001")]}
        page2 = {"status": "000", "total_count": 2, "total_page": 2, "list": [self._row("20260101000002")]}
        http = FakeHttp([page1, page2])
        client = OpenDartClient("masked", http=http)  # type: ignore[arg-type]
        diagnostics = SearchExecutionDiagnostics()
        result = client.collect_lists(date_from=date(2026, 1, 1), date_to=date(2026, 1, 2), diagnostics=diagnostics)
        self.assertEqual([x.receipt_no for x in result.candidates], ["20260102000001", "20260101000002"])
        self.assertEqual(diagnostics.actual_list_requests, 2)
        params = http.requests[0][2]["params"]
        self.assertEqual(params["page_count"], 100)
        self.assertEqual(params["sort"], "date")
        self.assertEqual(params["sort_mth"], "desc")

    def test_013_is_empty_healthy_window(self):
        http = FakeHttp([{"status": "013", "message": "조회된 데이타가 없습니다."}])
        diagnostics = SearchExecutionDiagnostics()
        result = OpenDartClient("masked", http=http).collect_lists(date_from=date(2026, 1, 1), date_to=date(2026, 1, 1), diagnostics=diagnostics)  # type: ignore[arg-type]
        self.assertTrue(result.complete)
        self.assertEqual(result.candidates, [])
        self.assertEqual(diagnostics.processed_window_count, 1)

    def test_status_900_retries_once_but_020_does_not(self):
        http = FakeHttp([
            {"status": "900", "message": "undefined"},
            {"status": "013", "message": "none"},
        ])
        client = OpenDartClient("masked", http=http)  # type: ignore[arg-type]
        payload = client.list_page(date_from=date(2026, 1, 1), date_to=date(2026, 1, 1))
        self.assertEqual(payload["status"], "013")
        self.assertEqual(len(http.requests), 2)
        limited = FakeHttp([{"status": "020", "message": "limit"}])
        with self.assertRaises(SearchError) as caught:
            OpenDartClient("masked", http=limited).list_page(date_from=date(2026, 1, 1), date_to=date(2026, 1, 1))  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, ErrorCode.OPENDART_REQUEST_LIMIT_EXCEEDED)
        self.assertEqual(len(limited.requests), 1)

    def test_rm_and_multiple_prefixes_preserve_order(self):
        self.assertEqual(parse_rm("공정X채"), (("공", "정", "채"), ("X",)))
        prefixes, name, unknown = parse_report_name("[정정제출요구][기재정정]증권신고서(지분증권)")
        self.assertEqual(prefixes, ("[정정제출요구]", "[기재정정]"))
        self.assertEqual(name, "증권신고서(지분증권)")
        self.assertFalse(unknown)

    def test_candidate_preserves_bond_flag_and_viewer_link(self):
        candidate = candidate_from_list_row(self._row("20260102000001", rm="채X"))
        self.assertEqual(candidate.rm_raw, "채X")
        self.assertEqual(candidate.rm_flags, ("채",))
        self.assertEqual(candidate.unknown_rm_flags, ("X",))
        self.assertIn("rcpNo=20260102000001", candidate.dart_viewer_url)
        self.assertEqual(dart_viewer_url("20260102000001"), candidate.dart_viewer_url)

    def test_document_parser_and_evidence(self):
        import io, zipfile
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("doc.xml", "<DOCUMENT><P>주금납입채무와 상계하여 상계납입한다.</P></DOCUMENT>")
        text = normalize_document_zip(stream.getvalue())
        snippets = extract_evidence("20260102000001", text, ["상계납입"])
        self.assertEqual(len(snippets), 1)
        self.assertIn("상계납입", snippets[0].text)

    @staticmethod
    def _row(receipt, rm=""):
        return {"corp_code": "001", "corp_name": "테스트", "stock_code": "", "corp_cls": "E", "report_nm": "보고서", "rcept_no": receipt, "flr_nm": "제출인", "rcept_dt": receipt[:8], "rm": rm}


if __name__ == "__main__":
    unittest.main()
