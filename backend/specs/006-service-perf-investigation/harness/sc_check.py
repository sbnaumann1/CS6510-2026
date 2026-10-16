"""Print the spec 006 success-criteria numbers for one or more run directories.

Usage (from backend/): python3 specs/006-service-perf-investigation/harness/sc_check.py .perf-runs/<run> ...
"""
import glob
import json
import os
import sys

for out in sys.argv[1:]:
    rep = glob.glob(os.path.join(out, "report-*.json"))
    if not rep:
        print(f"{out}: no report")
        continue
    d = json.load(open(rep[0]))
    ops = {o["operation"]: o for o in d["operations"]}
    c, s = ops["COMPLETE_TRANSACTION"], ops["SCAN_ITEM"]
    completes = c["successCount"] + c["errorCount"]
    begins = deadlocks = None
    stm = os.path.join(out, "pg_statements.tsv")
    if os.path.exists(stm):
        begins = sum(int(l.split("\t")[0]) for l in open(stm) if "BEGIN ISOLATION" in l)
    db = os.path.join(out, "pg_db.tsv")
    if os.path.exists(db):
        rows = [l.split("\t") for l in open(db) if l[:1].isdigit()]
        deadlocks = int(rows[0][2]) if rows else None
    log = os.path.join(out, "client.log")
    timeouts = open(log).read().count("timed out") if os.path.exists(log) else None
    inv = open(os.path.join(out, "invariant.txt")).read() if os.path.exists(os.path.join(out, "invariant.txt")) else ""
    print(
        f"{os.path.basename(out)}: tx/s={d['transactionsPerSecond']:.1f} items/s={d['itemsPerSecond']:.0f} "
        f"scan p50/p95={s['p50Ms']:.2f}/{s['p95Ms']:.2f} complete p50/p95/p99={c['p50Ms']:.1f}/{c['p95Ms']:.1f}/{c['p99Ms']:.1f} "
        f"cerr={c['errorRate']:.1%} | BEGIN={begins} completes={completes} "
        f"BEGIN/complete={(begins / completes) if begins is not None and completes else float('nan'):.2f} "
        f"deadlocks={deadlocks} timeouts={timeouts} invariant={'PASS' if 'exit=0' in inv else 'n/a' if not inv else 'FAIL'}"
    )
