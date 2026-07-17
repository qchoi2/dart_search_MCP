from __future__ import annotations

import argparse
import json
from pathlib import Path

from .session_lifecycle import SessionLifecycleProbe, write_probe_result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = SessionLifecycleProbe().run()
    if args.output:
        write_probe_result(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
