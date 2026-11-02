#!/usr/bin/env python
"""One-table comparison of the load-client reports written by scripts/run_profile.sh."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(directory: str) -> int:
    rows = []
    for f in sorted(Path(directory).glob("*.json")):
        d = json.loads(f.read_text())
        ops = {o["operation"]: o for o in d["operations"]}
        c = ops["COMPLETE_TRANSACTION"]
        rows.append((f.stem, d["transactionsPerSecond"], ops["SCAN_ITEM"]["meanMs"], c["meanMs"],
                     c["p50Ms"], c["p95Ms"], c["p99Ms"], c["errorCount"]))
    print(f"{'config-mode':22} {'tx/s':>7} {'scan ms':>8} {'COMPLETE mean':>14} {'p50':>7} {'p95':>7} {'p99':>7} {'errors':>7}")
    for r in rows:
        print(f"{r[0]:22} {r[1]:7.1f} {r[2]:8.2f} {r[3]:14.1f} {r[4]:7.1f} {r[5]:7.1f} {r[6]:7.1f} {r[7]:7d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "logs/profile"))
