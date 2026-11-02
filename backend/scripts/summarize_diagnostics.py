#!/usr/bin/env python
"""Saturation summary for one run_profile.sh run (spec 009 profiling).

    python scripts/summarize_diagnostics.py logs/profile/baseline-stress

Reads <prefix>.log (compose logs with TRACE_TIMING=1: the `monitor` lines) and
<prefix>.stats.csv (scripts/sample_stats.sh) and prints
  * CPU per container (mean and max, in cores) against the cores Docker gives the VM,
  * per service: event-loop lag and database-pool use and wait.
"""

from __future__ import annotations

import csv
import json
import re
import statistics
import subprocess
import sys
from collections import defaultdict

LINE = re.compile(r"(\{\"request_id\".*\})\s*$")


def main(prefix: str) -> int:
    try:
        ncpu = int(subprocess.run(["docker", "info", "--format", "{{.NCPU}}"],
                                  capture_output=True, text=True).stdout.strip())
    except ValueError:
        ncpu = 0

    cpu: dict[str, list[float]] = defaultdict(list)
    by_time: dict[int, float] = defaultdict(float)
    try:
        with open(prefix + ".stats.csv") as fh:
            for ts, name, c, _mem in csv.reader(fh):
                try:
                    v = float(c)
                except ValueError:
                    continue
                cpu[name.removeprefix("backend-").removesuffix("-1")].append(v)
                by_time[int(ts)] += v
    except FileNotFoundError:
        print("(no stats file)")
    if cpu:
        print(f"CPU per container, in cores (Docker VM has {ncpu or '?'}):")
        for name in sorted(cpu, key=lambda n: -statistics.mean(cpu[n])):
            v = cpu[name]
            print(f"  {name:14} mean={statistics.mean(v) / 100:5.2f}  max={max(v) / 100:5.2f}  (n={len(v)})")
        busy = [t for t in by_time.values()]
        # Samples are taken container by container over a second or two, so the sum per
        # timestamp is approximate; the mean over the busy half of the run is the useful figure.
        busy.sort()
        top = busy[len(busy) // 2:]
        print(f"  TOTAL (busier half of the samples): mean={statistics.mean(top) / 100:5.2f} cores,"
              f" max={max(busy) / 100:5.2f}\n")

    try:
        with open(prefix + ".stats.locks.csv") as fh:
            waits = [int(row[1]) for row in csv.reader(fh) if len(row) == 2]
        if waits:
            print(f"inventory-db backends waiting on a lock ({len(waits)} samples, one per second): "
                  f"mean={statistics.mean(waits):.2f} max={max(waits)} "
                  f"nonzero in {sum(1 for w in waits if w) / len(waits):.0%} of samples\n")
    except FileNotFoundError:
        pass

    mon: dict[tuple[str, int], list[dict]] = defaultdict(list)
    with open(prefix + ".log", errors="replace") as fh:
        for line in fh:
            m = LINE.search(line)
            if not m:
                continue
            try:
                d = json.loads(m.group(1))
            except ValueError:
                continue
            if d.get("step") == "monitor":
                mon[(d["service"], d["pid"])].append(d)
    if not mon:
        print("no monitor lines (TRACE_TIMING=1 required)")
        return 0
    print("Per worker process, mean over 5 s intervals that saw traffic "
          "(lag = how late a 10 ms timer fires):")
    print(f"  {'service/pid':16} {'loop lag mean/p99/max ms':>26} {'pool used mean/max of size':>28}"
          f" {'full %':>7} {'pool wait mean/p99 ms':>22} {'gets/5s':>8}")
    for (svc, pid), rows in sorted(mon.items()):
        rows = [r for r in rows if r["pool_gets"] > 20] or rows
        def m(k): return statistics.mean(r[k] for r in rows)
        print(f"  {svc + '/' + str(pid):16} {m('lag_mean_ms'):8.2f}/{m('lag_p99_ms'):6.1f}/"
              f"{max(r['lag_max_ms'] for r in rows):8.1f} "
              f"{m('pool_used_mean'):14.1f}/{max(r['pool_used_max'] for r in rows):3d} of {rows[0]['pool_size']:<3d}"
              f" {m('pool_full_frac') * 100:7.1f} {m('pool_wait_mean_ms'):12.2f}/{m('pool_wait_p99_ms'):7.1f}"
              f" {m('pool_gets'):8.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
