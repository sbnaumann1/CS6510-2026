#!/usr/bin/env python
"""Per-step timings from the services' structured logs (spec 007 FR-020, research R12).

    docker compose logs --no-color > run.log        # or logs/compose-stress.log, written
    python scripts/step_timings.py run.log          # by scripts/run_load_tests.sh

Every coordination step and outbound call logs one JSON line with `service`, `step`,
`outcome` and `elapsed_ms`. This prints, per (service, step, outcome) with at least 50
samples, the count, mean and the p50/p95/p99 in milliseconds. It is how the completion
saga's cost is attributed to its steps in load-client/reports/README.md.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict

MIN_SAMPLES = 50


def collect(lines) -> dict[tuple[str, str, str], list[float]]:
    by: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for line in lines:
        m = re.search(r"(\{\"request_id\".*\})\s*$", line)
        if not m:
            continue
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if d.get("elapsed_ms") is None:
            continue
        by[(d["service"], d["step"], d["outcome"])].append(d["elapsed_ms"])
    return by


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <compose log file>", file=sys.stderr)
        return 2
    with open(argv[1], errors="replace") as fh:
        by = collect(fh)
    for key in sorted(by):
        v = sorted(by[key])
        if len(v) < MIN_SAMPLES:
            continue

        def q(p: float) -> float:
            return v[min(len(v) - 1, int(len(v) * p))]

        print(
            f"{key[0]:10} {key[1]:10} {key[2]:10} n={len(v):6} mean={statistics.mean(v):7.2f} "
            f"p50={q(0.5):7.2f} p95={q(0.95):7.2f} p99={q(0.99):7.2f} ms"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
