"""Stage 5 cache benchmark with explicit pass/fail thresholds.

The caller supplies normalized documents and measured network/parse baseline
seconds.  This keeps the cache benchmark deterministic while allowing the
report to distinguish fixture-only runs from a limited live measurement.
"""

from __future__ import annotations

import statistics
import tempfile
import time
from pathlib import Path
from typing import Iterable

from app.storage.session_cache import SessionTextCache
from app.storage.ttl_cache import DiskTtlTextCache, TieredTextCache


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile + 0.999999)))
    return ordered[index]


def _receipt(index: int) -> str:
    return f"20260717{index:06d}"


def benchmark_cache_modes(
    documents: Iterable[str],
    *,
    baseline_seconds: float,
    measurement_basis: str,
) -> dict:
    docs = list(documents)
    if not docs:
        raise ValueError("at least one document is required")
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="dart_stage5_") as raw:
        root = Path(raw)
        for label, compression in (("A_session_only", None), ("B_ttl_uncompressed", "none"), ("C_ttl_gzip1", "gzip1"), ("D_no_cache", "disabled")):
            write_ms: list[float] = []
            read_ms: list[float] = []
            disk = None if compression in {None, "disabled"} else DiskTtlTextCache(root / label, compression=compression)
            cache = None if compression == "disabled" else TieredTextCache(SessionTextCache(), disk)
            first_start = time.perf_counter()
            for index, text in enumerate(docs):
                if cache is not None:
                    started = time.perf_counter()
                    cache.put(_receipt(index), text)
                    write_ms.append((time.perf_counter() - started) * 1000)
            cache_elapsed = time.perf_counter() - first_start
            first_seconds = baseline_seconds + cache_elapsed
            repeat_cache = None
            if cache is not None:
                # A retains its session; B/C emulate a process restart to prove
                # that the disk tier is the source of repeat-search acceleration.
                repeat_cache = cache if disk is None else TieredTextCache(SessionTextCache(), disk)
            repeat_start = time.perf_counter()
            for index, text in enumerate(docs):
                started = time.perf_counter()
                if repeat_cache is None:
                    _ = text.casefold().count("정정")
                else:
                    assert repeat_cache.get(_receipt(index)) == text
                read_ms.append((time.perf_counter() - started) * 1000)
            repeat_seconds = time.perf_counter() - repeat_start
            disk_bytes = sum(path.stat().st_size for path in (root / label).rglob("*") if path.is_file()) if disk else 0
            rows.append({
                "mode": label,
                "first_search_seconds": round(first_seconds, 6),
                "repeat_search_seconds": round(repeat_seconds, 6),
                "write_p50_ms": round(statistics.median(write_ms), 3) if write_ms else 0.0,
                "write_p95_ms": round(_percentile(write_ms, 0.95), 3),
                "read_p95_ms": round(_percentile(read_ms, 0.95), 3),
                "disk_bytes": disk_bytes,
            })
        # Corruption must be a recoverable miss, not a search failure.
        corrupt = DiskTtlTextCache(root / "corrupt", compression="gzip1")
        corrupt.put(_receipt(999), docs[0])
        corrupt_path = corrupt._path(_receipt(999))
        corrupt_path.write_bytes(b"not-a-gzip-stream")
        corruption_recovered = corrupt.get(_receipt(999)) is None and not corrupt_path.exists()
    session_first = next(row["first_search_seconds"] for row in rows if row["mode"] == "A_session_only")
    candidates = []
    for row in rows:
        if row["mode"] not in {"B_ttl_uncompressed", "C_ttl_gzip1"}:
            continue
        increase = max(0.0, (row["first_search_seconds"] - session_first) / max(session_first, 1e-9) * 100)
        row["first_search_increase_percent"] = round(increase, 3)
        row["passes"] = increase <= 5.0 and row["write_p95_ms"] <= 100.0 and corruption_recovered
        candidates.append(row)
    passing = [row for row in candidates if row["passes"]]
    selected = min(passing, key=lambda row: (row["disk_bytes"], row["first_search_seconds"]), default=None)
    return {
        "measurement_basis": measurement_basis,
        "document_count": len(docs),
        "baseline_seconds": round(baseline_seconds, 6),
        "thresholds": {"max_first_search_increase_percent": 5.0, "max_write_p95_ms": 100.0},
        "modes": rows,
        "corruption_recovered": corruption_recovered,
        "ttl_gate_passed": selected is not None,
        "selected_mode": selected["mode"] if selected else None,
    }
