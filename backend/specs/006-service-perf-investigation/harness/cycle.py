"""Where a station's time goes per transaction (closed-loop accounting).

For each run directory: mean ms per transaction spent in start, scans and complete,
from the load-client report. Usage: python3 cycle.py <run-dir> ...
"""
import glob
import json
import os
import sys

for out in sys.argv[1:]:
    d = json.load(open(glob.glob(os.path.join(out, "report-*.json"))[0]))
    ops = {o["operation"]: o for o in d["operations"]}
    starts = ops["START_TRANSACTION"]["successCount"]
    parts = {
        k: ops[k]["meanMs"] * (ops[k]["successCount"] + ops[k]["errorCount"]) / starts
        for k in ("START_TRANSACTION", "SCAN_ITEM", "COMPLETE_TRANSACTION")
    }
    total = sum(parts.values())
    print(
        f"{os.path.basename(out)}: per-tx ms start={parts['START_TRANSACTION']:.1f} "
        f"scans={parts['SCAN_ITEM']:.1f} complete={parts['COMPLETE_TRANSACTION']:.1f} "
        f"total={total:.1f} (complete share {parts['COMPLETE_TRANSACTION'] / total:.0%}); "
        f"stations/total = {d['config']['stations'] / total * 1000:.0f} tx/s implied, {d['transactionsPerSecond']:.0f} measured"
    )
