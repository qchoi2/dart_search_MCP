from __future__ import annotations

import argparse
from pathlib import Path

from app.probe.common import find_api_key

from .runner import Stage06Probe, new_run_id, rebuild_current_golden


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run DEVELOPMENT_PLAN v18 stage-0.6 probes only; "
            "this command never starts stage 1."
        )
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--deadline-seconds", type=int, default=900)
    parser.add_argument("--min-interval", type=float, default=1.0)
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="measure only the three DART web gates; OpenDART gates remain unconfirmed",
    )
    parser.add_argument(
        "--rebuild-current-golden",
        action="store_true",
        help="recompute curated golden findings from the current completed raw run without network",
    )
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 60:
        parser.error("--max-requests must be between 1 and the stage-0.6 cap of 60")
    if args.deadline_seconds < 60:
        parser.error("--deadline-seconds must be at least 60")
    if args.min_interval < 1.0:
        parser.error("--min-interval must be at least 1.0")

    if args.rebuild_current_golden:
        rebuild_current_golden(_repo_root())
        return 0

    api_key, source = find_api_key()
    if not api_key and not args.web_only:
        raise SystemExit(
            "OpenDART API key is not set. Set DART_API_KEY for this process and rerun."
        )
    with Stage06Probe(
        repo_root=_repo_root(),
        api_key=api_key or "",
        api_key_source=source,
        run_id=args.run_id or new_run_id(),
        max_requests=args.max_requests,
        deadline_seconds=args.deadline_seconds,
        min_interval=args.min_interval,
    ) as probe:
        probe.manifest["execution_mode"] = "web_only" if args.web_only else "all"
        probe.run(web_only=args.web_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
