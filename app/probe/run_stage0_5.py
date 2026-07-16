from __future__ import annotations

import argparse
from pathlib import Path

from .common import find_api_key
from .stage0_5 import Stage05Probe, new_run_id


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run DEVELOPMENT_PLAN v16 stage-0.5 probes only; never starts stage 1."
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--deadline-seconds", type=int, default=1200)
    parser.add_argument("--min-interval", type=float, default=1.0)
    args = parser.parse_args()
    if args.max_requests < 1:
        parser.error("--max-requests must be positive")
    if args.deadline_seconds < 60:
        parser.error("--deadline-seconds must be at least 60")
    if args.min_interval < 1.0:
        parser.error("--min-interval must be at least 1.0 for DART web requests")

    api_key, source = find_api_key()
    if not api_key:
        raise SystemExit(
            "OpenDART API key is not set. Set DART_API_KEY for this process and rerun."
        )
    run_id = args.run_id or new_run_id()
    with Stage05Probe(
        repo_root=_repo_root(),
        api_key=api_key,
        run_id=run_id,
        max_requests=args.max_requests,
        deadline_seconds=args.deadline_seconds,
        min_interval=args.min_interval,
    ) as probe:
        probe.manifest["api_key_source"] = source
        probe.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
