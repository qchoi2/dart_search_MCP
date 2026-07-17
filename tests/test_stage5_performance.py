from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from app.channels.adaptive import AdaptiveConcurrency
from app.channels.opendart import OpenDartClient
from app.config import defaults
from app.contracts import SearchExecutionDiagnostics
from app.errors import ErrorCode, SearchError
from app.performance.stage5 import benchmark_cache_modes
from app.storage.session_cache import SessionTextCache
from app.storage.ttl_cache import DiskTtlTextCache, TieredTextCache


class Stage5CacheTests(unittest.TestCase):
    def test_ttl_cache_round_trip_expiry_and_corruption_recovery(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as raw:
            cache = DiskTtlTextCache(Path(raw), ttl_hours=1, compression="gzip1", clock=lambda: now[0])
            cache.put("20260717000001", "정정 전 100 정정 후 120")
            self.assertEqual(cache.get("20260717000001"), "정정 전 100 정정 후 120")
            path = cache._path("20260717000001")
            path.write_bytes(b"corrupt")
            self.assertIsNone(cache.get("20260717000001"))
            self.assertEqual(cache.corruptions, 1)
            cache.put("20260717000001", "fresh")
            now[0] += 3600
            self.assertIsNone(cache.get("20260717000001"))

    def test_tiered_session_mode_can_bypass_disk(self):
        with tempfile.TemporaryDirectory() as raw:
            disk = DiskTtlTextCache(Path(raw), compression="gzip1")
            cache = TieredTextCache(SessionTextCache(), disk)
            cache.put_session("20260717000002", "session-only")
            self.assertEqual(cache.get_session("20260717000002"), "session-only")
            self.assertEqual(list(Path(raw).rglob("*.json.gz")), [])

    def test_cache_gate_compares_all_four_modes(self):
        docs = [("정정사항 납입일 2026.07.17 " * 500), ("전환가액 1000원 " * 500)]
        result = benchmark_cache_modes(docs, baseline_seconds=1.0, measurement_basis="fixture")
        self.assertEqual({row["mode"] for row in result["modes"]}, {
            "A_session_only", "B_ttl_uncompressed", "C_ttl_gzip1", "D_no_cache"
        })
        self.assertTrue(result["corruption_recovered"])
        self.assertTrue(result["ttl_gate_passed"])


class Stage5ConcurrencyTests(unittest.TestCase):
    def test_list_first_pages_use_configured_concurrency_two(self):
        class FixtureClient(OpenDartClient):
            def __init__(self):
                self.requests_started = 0
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()
                self.barrier = threading.Barrier(2)

            def list_page(self, **kwargs):
                del kwargs
                with self.lock:
                    self.requests_started += 1
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    self.barrier.wait(timeout=1)
                    return {"status": "013", "message": "조회된 데이타가 없습니다."}
                finally:
                    with self.lock:
                        self.active -= 1

        client = FixtureClient()
        diagnostics = SearchExecutionDiagnostics()
        collection = client.collect_lists(
            date_from=date(2026, 1, 1),
            date_to=date(2026, 7, 17),
            diagnostics=diagnostics,
            request_budget=2,
            list_concurrency=2,
        )
        self.assertEqual(client.max_active, 2)
        self.assertEqual(diagnostics.actual_list_requests, 2)
        self.assertFalse(collection.complete)

    def test_adaptive_document_concurrency_is_three_two_one(self):
        state = AdaptiveConcurrency(defaults.DOCUMENT_CONCURRENCY)
        self.assertEqual(state.current, 3)
        self.assertTrue(state.observe(SearchError(ErrorCode.OPENDART_HTTP_RATE_LIMITED, "rate")))
        self.assertEqual(state.current, 2)
        self.assertTrue(state.observe(SearchError(ErrorCode.OPENDART_TEMPORARY_FAILURE, "timeout")))
        self.assertEqual(state.current, 1)
        self.assertFalse(state.observe(SearchError(ErrorCode.OPENDART_TEMPORARY_FAILURE, "5xx")))

    def test_status_020_is_immediate_stop_not_adaptive_slowdown(self):
        state = AdaptiveConcurrency(defaults.DOCUMENT_CONCURRENCY)
        changed = state.observe(SearchError(ErrorCode.OPENDART_REQUEST_LIMIT_EXCEEDED, "020"))
        self.assertFalse(changed)
        self.assertEqual(state.current, 3)


if __name__ == "__main__":
    unittest.main()
