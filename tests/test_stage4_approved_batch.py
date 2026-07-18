from __future__ import annotations

import json
import hashlib
import csv
import io
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path

from app.batch.service import BatchResearchService
from app.channels.health import CircuitBreaker
from app.channels.dart_fulltext import DartResultRow, DartSearchPage
from app.contracts import ChannelStatus
from app.errors import ErrorCode, SearchError
from app.mcp_server.server import McpApplication
from app.storage.batch_store import BatchPlanStore
from app.storage.audit_log import AuditLog
from app.security.csv_guard import escape_csv_cell


def row(receipt: str, text: str = "상계") -> dict:
    return {
        "rcept_no": receipt,
        "corp_code": "00126380",
        "corp_name": "테스트회사",
        "report_nm": f"주요사항보고서 {text}",
        "rcept_dt": "20260716",
        "flr_nm": "테스트회사",
        "rm": "",
    }


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class FakeOpenDart:
    def __init__(self, rows: list[dict], *, clock: Clock | None = None, advance_on_download: float = 0) -> None:
        self.rows = rows
        self.clock = clock
        self.advance_on_download = advance_on_download
        self.list_calls = 0
        self.download_calls = 0
        self.active_downloads = 0
        self.max_active_downloads = 0
        self.lock = threading.Lock()

    def list_page(self, **kwargs):
        self.list_calls += 1
        return {"status": "000", "list": list(self.rows), "total_count": len(self.rows), "total_page": 1}

    def download_document(self, receipt_no: str, **kwargs) -> str:
        with self.lock:
            self.download_calls += 1
            self.active_downloads += 1
            self.max_active_downloads = max(self.max_active_downloads, self.active_downloads)
        try:
            time.sleep(0.005)
            if self.clock is not None:
                with self.lock:
                    self.clock.value += self.advance_on_download
            return f"이 문서는 {receipt_no} 상계납입 사례입니다."
        finally:
            with self.lock:
                self.active_downloads -= 1


class RateLimitedOnceOpenDart(FakeOpenDart):
    def __init__(self, rows: list[dict]) -> None:
        super().__init__(rows)
        self.failed = False

    def download_document(self, receipt_no: str, **kwargs) -> str:
        if receipt_no.endswith("01") and not self.failed:
            self.failed = True
            with self.lock:
                self.download_calls += 1
            raise SearchError(ErrorCode.OPENDART_HTTP_RATE_LIMITED, "rate limited", True)
        return super().download_document(receipt_no, **kwargs)


class FakeDart:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.breaker = CircuitBreaker(clock=clock)
        self.health_calls = 0

    def health_check(self, diagnostics, **kwargs) -> bool:
        self.health_calls += 1
        self.breaker.success()
        return True


class FakeSearchDart(FakeDart):
    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self.search_calls = 0

    def search_page(self, query, date_from, date_to, diagnostics, **kwargs):
        self.search_calls += 1
        diagnostics.dart_result_page_requests += 1
        result_row = DartResultRow(
            receipt_no="20260716000001",
            corp_code="00126380",
            company="테스트회사",
            market=None,
            report_name="주요사항보고서 상계",
            report_name_prefixes=(),
            snippet="상계",
            disclosure_group=None,
            match_scope="body",
            filer_name="테스트회사",
            receipt_date="20260716",
            row_tags=(),
        )
        return DartSearchPage("normal_results", 1, (result_row,), (), 1, 1, 1, False)


class PagedSearchDart(FakeSearchDart):
    def search_page(self, query, date_from, date_to, diagnostics, **kwargs):
        page_no = int(kwargs.get("page", 1))
        self.clock.value += 280
        result = super().search_page(query, date_from, date_to, diagnostics, **kwargs)
        return DartSearchPage(
            result.classification,
            2,
            result.rows,
            result.zero_markers,
            page_no,
            2,
            2,
            False,
        )


class Engine:
    def __init__(self, opendart: FakeOpenDart, dart=None, audit=None) -> None:
        self.opendart = opendart
        self.dart = dart
        self.audit = audit


class Stage4BatchTests(unittest.TestCase):
    def test_stage4_fixture_manifest(self):
        fixture_root = Path(__file__).parent / "fixtures" / "stage4"
        manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
        for relative, expected in manifest["files"].items():
            digest = hashlib.sha256((fixture_root / relative).read_bytes()).hexdigest()
            self.assertEqual(digest, expected)

    def make_service(self, root: Path, channel: FakeOpenDart, clock: Clock | None = None, dart=None) -> BatchResearchService:
        active_clock = clock or Clock()
        plans = BatchPlanStore(clock=active_clock)
        return BatchResearchService(
            Engine(channel, dart),
            root=root,
            clock=active_clock,
            wall_clock=active_clock,
            plans=plans,
        )

    def preview(self, service: BatchResearchService) -> dict:
        return service.preview(
            query="상계",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 3, 31),
            target_count=100,
            exhaustive=True,
        )

    def test_preview_is_metadata_only_and_reuses_same_lineage_plan(self):
        with tempfile.TemporaryDirectory() as raw:
            channel = FakeOpenDart([row("20260716000001")])
            service = self.make_service(Path(raw), channel)
            first = self.preview(service)
            second = self.preview(service)
            self.assertEqual(first["status"], "confirmation_required")
            self.assertEqual(first["plan_id"], second["plan_id"])
            self.assertTrue(second["plan_reused"])
            self.assertEqual(channel.list_calls, 1)
            self.assertEqual(channel.download_calls, 0)
            self.assertEqual(first["confirmation_interval_options_minutes"], [5, 10, 15, 30])
            self.assertEqual(
                first["dart_rate_floor_seconds"],
                max(0, first["estimated_dart_requests"] - 1),
            )

    def test_decline_and_invalid_interval_never_start_network(self):
        with tempfile.TemporaryDirectory() as raw:
            channel = FakeOpenDart([row("20260716000001")])
            service = self.make_service(Path(raw), channel)
            plan = self.preview(service)
            calls_after_preview = channel.list_calls
            invalid = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=7)
            self.assertEqual(invalid["status"], "confirmation_interval_required")
            self.assertEqual(channel.list_calls, calls_after_preview)
            declined = service.run(plan_id=plan["plan_id"], approved=False, confirmation_interval_minutes=5)
            self.assertEqual(declined["status"], "declined")
            self.assertFalse(declined["network_started"])
            self.assertEqual(channel.list_calls, calls_after_preview)
            suppressed = self.preview(service)
            self.assertEqual(suppressed["status"], "recommendation_suppressed")
            self.assertEqual(channel.list_calls, calls_after_preview)

    def test_scope_increase_of_fifty_percent_allows_new_preview(self):
        with tempfile.TemporaryDirectory() as raw:
            channel = FakeOpenDart([])
            service = self.make_service(Path(raw), channel)
            first = self.preview(service)
            self.assertEqual(first["estimated_documents"], 0)
            self.assertEqual(first["estimated_unique_documents"], 0)
            self.assertIsNone(first["dart_search_count"])
            service.run(plan_id=first["plan_id"], approved=False, confirmation_interval_minutes=5)
            expanded = service.preview(
                query="상계",
                date_from=date(2026, 1, 1),
                date_to=date(2026, 5, 15),
                target_count=100,
                exhaustive=True,
            )
            self.assertEqual(expanded["status"], "confirmation_required")
            self.assertNotEqual(expanded["plan_id"], first["plan_id"])

    def test_mcp_batch_response_has_schema_version(self):
        with tempfile.TemporaryDirectory() as raw:
            channel = FakeOpenDart([])
            engine = Engine(channel)
            service = self.make_service(Path(raw), channel)
            app = McpApplication(engine, service)
            result = app.call_tool(
                "preview_batch_research",
                {"query": "상계", "date_from": "2026-01-01", "date_to": "2026-03-31"},
            )
            self.assertEqual(result["schema_version"], "1.0")
            self.assertEqual(result["feature"], "deep_search")
            self.assertEqual(result["feature_label"], "공시 MCP의 심화 검색기능")
            self.assertIn("물어봐 주세요", result["help"])

    def test_invalid_plan_never_starts_network(self):
        with tempfile.TemporaryDirectory() as raw:
            channel = FakeOpenDart([])
            service = self.make_service(Path(raw), channel)
            result = service.run(plan_id="missing-plan", approved=True, confirmation_interval_minutes=5)
            self.assertEqual(result["status"], "invalid_or_expired_plan")
            self.assertEqual(channel.list_calls, 0)

    def test_approved_batch_completes_and_stores_query_hash_only(self):
        with tempfile.TemporaryDirectory() as raw:
            channel = FakeOpenDart([row("20260716000001")])
            service = self.make_service(Path(raw), channel)
            plan = self.preview(service)
            result = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["soft_deadline_seconds"], 270)
            self.assertEqual(result["hard_deadline_seconds"], 300)
            record = service.records.load(result["search_record_id"])
            self.assertIsNone(record["request"]["query"])
            self.assertTrue(record["request"]["normalized_query_hash"])
            self.assertEqual(result["result_count"], 1)
            self.assertTrue(record["results"][0]["original_document_url"].endswith("20260716000001"))

    def test_interval_checkpoint_and_explicit_continuation(self):
        with tempfile.TemporaryDirectory() as raw:
            clock = Clock()
            channel = FakeOpenDart(
                [row("20260716000001"), row("20260716000002"), row("20260716000003"), row("20260716000004")],
                clock=clock,
                advance_on_download=280,
            )
            service = self.make_service(Path(raw), channel, clock)
            plan = self.preview(service)
            first = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(first["status"], "continuation_confirmation_required")
            self.assertEqual(first["checkpoint"]["next_row_offset"], 3)
            before = channel.download_calls
            declined = service.continue_run(job_id=first["job_id"], approved=False, confirmation_interval_minutes=5)
            self.assertEqual(declined["status"], "continuation_declined")
            self.assertEqual(channel.download_calls, before)
            completed = service.continue_run(job_id=first["job_id"], approved=True, confirmation_interval_minutes=10)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["result_count"], 4)

    def test_export_requires_path_then_writes_csv_and_json(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            channel = FakeOpenDart([row("20260716000001")])
            service = self.make_service(root / "state", channel)
            plan = self.preview(service)
            completed = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            missing = service.export(search_record_id=completed["search_record_id"], formats=["csv"], output_directory=None)
            self.assertEqual(missing["status"], "clarification_required")
            self.assertEqual(missing["files_written"], [])
            output = root / "output"
            exported = service.export(
                search_record_id=completed["search_record_id"],
                formats=["csv", "json"],
                output_directory=str(output),
            )
            self.assertEqual(exported["status"], "completed")
            self.assertEqual(len(exported["files_written"]), 2)
            payload = json.loads((output / f"{completed['search_record_id']}.json").read_text(encoding="utf-8"))
            self.assertIsNone(payload["request"]["query"])
            self.assertTrue(payload["export_safety"]["json_preserves_original_text"])

    def test_checkpoint_store_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as raw:
            service = self.make_service(Path(raw), FakeOpenDart([]))
            with self.assertRaises(ValueError):
                service.checkpoints.load("../outside")

    def test_expired_checkpoint_circuit_is_probed_once_on_resume(self):
        with tempfile.TemporaryDirectory() as raw:
            clock = Clock()
            channel = FakeOpenDart(
                [row("20260716000001"), row("20260716000002"), row("20260716000003"), row("20260716000004")],
                clock=clock,
                advance_on_download=280,
            )
            dart = FakeDart(clock)
            dart.breaker.trip("structure_or_access")
            service = self.make_service(Path(raw), channel, clock, dart)
            plan = self.preview(service)
            first = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(first["status"], "continuation_confirmation_required")
            clock.value = 2000
            completed = service.continue_run(job_id=first["job_id"], approved=True, confirmation_interval_minutes=10)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(dart.health_calls, 1)
            self.assertEqual(dart.breaker.state.status, ChannelStatus.HEALTHY)

    def test_document_requests_use_stage5_concurrency_and_rate_limit_slows_down(self):
        rows = [row(f"2026071600000{index}") for index in range(1, 5)]
        with tempfile.TemporaryDirectory() as raw:
            channel = RateLimitedOnceOpenDart(rows)
            service = self.make_service(Path(raw), channel)
            plan = self.preview(service)
            stopped = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(stopped["status"], "continuation_confirmation_required")
            self.assertEqual(stopped["stop_reason"], ErrorCode.OPENDART_HTTP_RATE_LIMITED.value)
            self.assertLessEqual(channel.max_active_downloads, 3)
            checkpoint = service.checkpoints.load(stopped["job_id"])
            self.assertEqual(checkpoint["document_concurrency"], 2)
            self.assertEqual(checkpoint["document_concurrency_events"][-1]["from"], 3)
            self.assertEqual(checkpoint["document_concurrency_events"][-1]["to"], 2)
            completed = service.continue_run(job_id=stopped["job_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(completed["status"], "completed")
            self.assertLessEqual(channel.max_active_downloads, 3)

    def test_csv_formula_prefixes_are_escaped(self):
        for value in ("=1+1", "+SUM(A1:A2)", "-2+3", "@cmd", "\tformula", "\rformula"):
            self.assertEqual(escape_csv_cell(value), "'" + value)
        raw = BatchResearchService._csv_bytes(
            [{
                "receipt_no": "20260716000001",
                "corp_name": "@company",
                "report_name": "=HYPERLINK(\"bad\")",
                "receipt_date": "20260716",
                "viewer_url": "+unsafe",
                "evidence": [{"text": "-SUM(A1:A2)"}],
            }]
        )
        parsed = next(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertTrue(parsed["corp_name"].startswith("'@"))
        self.assertTrue(parsed["report_name"].startswith("'="))
        self.assertTrue(parsed["viewer_url"].startswith("'+"))
        self.assertTrue(parsed["evidence"].startswith("'-"))

    def test_dart_date_window_candidates_drive_batch_verification(self):
        with tempfile.TemporaryDirectory() as raw:
            clock = Clock()
            channel = FakeOpenDart([row("20260716000099")])
            dart = FakeSearchDart(clock)
            service = self.make_service(Path(raw), channel, clock, dart)
            plan = self.preview(service)
            preview_list_calls = channel.list_calls
            completed = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["result_count"], 1)
            self.assertEqual(channel.list_calls, preview_list_calls)
            self.assertGreaterEqual(dart.search_calls, 2)
            self.assertEqual(channel.download_calls, 1)

    def test_dart_page_checkpoint_resumes_at_exact_page(self):
        with tempfile.TemporaryDirectory() as raw:
            clock = Clock()
            channel = FakeOpenDart([])
            dart = PagedSearchDart(clock)
            service = self.make_service(Path(raw), channel, clock, dart)
            plan = service.preview(
                query="희귀문구",
                date_from=date(2026, 1, 1),
                date_to=date(2026, 3, 31),
                target_count=100,
                exhaustive=True,
            )
            first = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(first["status"], "continuation_confirmation_required")
            self.assertEqual(first["checkpoint"]["phase"], "dart_discovery")
            self.assertEqual(first["checkpoint"]["dart_next_page"], 2)

    def test_batch_response_exposes_open_circuit_deadline(self):
        with tempfile.TemporaryDirectory() as raw:
            clock = Clock()
            channel = FakeOpenDart([])
            dart = FakeDart(clock)
            dart.breaker.trip("structure_or_access")
            service = self.make_service(Path(raw), channel, clock, dart)
            plan = self.preview(service)
            completed = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(completed["status"], "completed")
            self.assertGreater(completed["blocked_seconds"], 0)
            self.assertIsNotNone(completed["blocked_until"])

    def test_completed_batch_writes_minimized_audit_summary(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audit = AuditLog(root / "audit.jsonl")
            channel = FakeOpenDart([row("20260716000001")])
            engine = Engine(channel, audit=audit)
            clock = Clock()
            service = BatchResearchService(
                engine,
                root=root / "state",
                clock=clock,
                wall_clock=clock,
                plans=BatchPlanStore(clock=clock),
            )
            plan = self.preview(service)
            service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            record = json.loads((root / "audit.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["mode"], "approved_batch")
            self.assertNotIn("query", record)
            self.assertTrue(record["normalized_query_hash"])



class MnaOpenDart(FakeOpenDart):
    def __init__(self, docs: dict[str, str], receipts: list[str]) -> None:
        super().__init__([row(receipt) for receipt in receipts])
        self.docs = docs
        self.list_disclosure_types: list[str | None] = []

    def list_page(self, **kwargs):
        self.list_disclosure_types.append(kwargs.get("disclosure_type"))
        return super().list_page(**kwargs)

    def download_document(self, receipt_no: str, **kwargs) -> str:
        with self.lock:
            self.download_calls += 1
        return self.docs.get(receipt_no, "")


class Stage4TitleConstraintBatchTests(unittest.TestCase):
    MNA_QUERY = "공개매수가 완료될 것을 전제로 한 주식매매계약의 거래종결 사례를 찾아줘"

    def test_batch_scopes_pool_to_d004_and_verifies_by_cooccurrence(self):
        with tempfile.TemporaryDirectory() as raw:
            clock = Clock()
            docs = {
                "20240701000001": "공개매수 완료를 전제로 한 주식매매계약의 거래종결 조항이 있다.",
                "20240701000002": "공개매수 관련 주식매매계약 체결 사실을 공시한다.",
            }
            channel = MnaOpenDart(docs, list(docs.keys()))
            dart = FakeSearchDart(clock)
            service = BatchResearchService(
                Engine(channel, dart),
                root=Path(raw),
                clock=clock,
                wall_clock=clock,
                plans=BatchPlanStore(clock=clock),
            )
            plan = service.preview(
                query=self.MNA_QUERY,
                date_from=date(2024, 1, 1),
                date_to=date(2024, 12, 31),
                target_count=10,
                exhaustive=True,
            )
            # Pool is scoped to the D004 detail type; report(title) mode stays off
            # and the DART body channel is idle for both estimate and execution.
            self.assertEqual(plan["scope"]["disclosure_types"], ["D004"])
            self.assertEqual(plan["dart_query"]["mode"], "contents")
            self.assertEqual(plan["estimated_dart_requests"], 0)
            self.assertGreaterEqual(len(plan["dart_query"]["verification_term_groups"]), 4)
            completed = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(completed["status"], "completed")
            # Only the document where every concept co-occurs is verified.
            self.assertEqual(completed["result_count"], 1)
            self.assertIn("D004", channel.list_disclosure_types)
            self.assertEqual(dart.search_calls, 0)
            record = service.records.load(completed["search_record_id"])
            self.assertEqual(record["results"][0]["receipt_no"], "20240701000001")



class Stage4PermanentDocumentFailureTests(unittest.TestCase):
    def test_permanent_document_failure_is_skipped_and_batch_completes(self):
        # A permanent per-document error (missing original file) must not rewind
        # the checkpoint and stall; the batch skips that receipt and finishes.
        class MissingDocOpenDart(FakeOpenDart):
            def download_document(self, receipt_no, **kwargs):
                if receipt_no.endswith("02"):
                    with self.lock:
                        self.download_calls += 1
                    raise SearchError(ErrorCode.OPENDART_FILE_NOT_FOUND, "원본 파일 없음")
                return super().download_document(receipt_no, **kwargs)

        with tempfile.TemporaryDirectory() as raw:
            clock = Clock()
            rows = [row("20240701000001"), row("20240701000002"), row("20240701000003")]
            channel = MissingDocOpenDart(rows)
            service = BatchResearchService(
                Engine(channel, dart=None), root=Path(raw),
                clock=clock, wall_clock=clock, plans=BatchPlanStore(clock=clock),
            )
            plan = service.preview(
                query="상계", date_from=date(2024, 7, 1), date_to=date(2024, 7, 31),
                target_count=10, exhaustive=True,
            )
            completed = service.run(plan_id=plan["plan_id"], approved=True, confirmation_interval_minutes=5)
            self.assertEqual(completed["status"], "completed")
            record = service.records.load(completed["search_record_id"])
            receipts = {item["receipt_no"] for item in record["results"]}
            self.assertNotIn("20240701000002", receipts)
            self.assertIn("20240701000001", receipts)
            self.assertIn("20240701000003", receipts)
            skipped = [d for d in record["diagnostics"] if d.get("action") == "skipped_permanent_document_failure"]
            self.assertTrue(any(d.get("receipt_no") == "20240701000002" for d in skipped))


if __name__ == "__main__":
    unittest.main()
