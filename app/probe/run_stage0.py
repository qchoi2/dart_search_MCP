from __future__ import annotations

import argparse
from pathlib import Path

from .common import find_api_key, read_json
from .dart_web import run_dart_web_probe
from .opendart import run_opendart_probe
from .reporting import combine_findings, render_outputs


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run DEVELOPMENT_PLAN stage-0 probes only (no product implementation)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--web-only", action="store_true", help="Run only the DART web probe")
    mode.add_argument(
        "--opendart-only", action="store_true", help="Run only OpenDART probes and reuse web findings"
    )
    parser.add_argument("--web-interval", type=float, default=1.0)
    parser.add_argument("--api-interval", type=float, default=0.35)
    parser.add_argument(
        "--fixture-root",
        type=Path,
        default=None,
        help="Defaults to tests/fixtures/probe under the repository root",
    )
    args = parser.parse_args()

    repo_root = _repo_root()
    fixture_root = (args.fixture_root or repo_root / "tests/fixtures/probe").resolve()
    fixture_root.mkdir(parents=True, exist_ok=True)

    web_findings = read_json(fixture_root / "dart_web/findings.json")
    opendart_findings = read_json(fixture_root / "opendart/findings.json")

    if not args.opendart_only:
        web_findings = run_dart_web_probe(fixture_root, min_interval=args.web_interval)

    if not args.web_only:
        api_key, api_key_source = find_api_key()
        if not api_key:
            combined = combine_findings(web_findings, None)
            render_outputs(repo_root, combined)
            raise SystemExit(
                "OpenDART API key is not set. Set DART_API_KEY (or OPENDART_API_KEY, "
                "OPEN_DART_API_KEY, CRTFC_KEY) and rerun with --opendart-only."
            )
        opendart_findings = run_opendart_probe(
            api_key,
            fixture_root,
            web_findings=web_findings,
            min_interval=args.api_interval,
        )
        opendart_findings["api_key_source"] = api_key_source

    combined = combine_findings(web_findings, opendart_findings if not args.web_only else None)
    render_outputs(repo_root, combined)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
