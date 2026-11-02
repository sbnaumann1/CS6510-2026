#!/usr/bin/env python
"""Break one completion into segments from TRACE_TIMING=1 logs (spec 009 profiling).

    docker compose logs --no-color > run.log
    python scripts/profile_completion.py run.log

Joins the `step: "trace"` lines (checkout_web.trace) by transaction id and prints, for each
segment of POST /transactions/{id}/complete, count, mean, p50, p95, p99 in ms. Segments are
listed in path order; `total` is the sum a shopper's request spends inside Checkout's saga.
Also prints the consumer loop's batch behaviour (size, handling time, offset-commit time,
wait before a record is handled) and how many events took a slow path (sweeper, re-publish).
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter, defaultdict

LINE = re.compile(r"(\{\"request_id\".*\})\s*$")

# (name, from-point, to-point). The first occurrence of each point per transaction is used.
SEGMENTS = [
    ("begin tx (lock, basket, mark COMPLETING, outbox)", "co-complete-start", "co-begin-committed"),
    ("-> publish call starts (task scheduling)", "co-begin-committed", "co-publish-start"),
    ("producer send_and_wait (linger + broker ack)", "co-publish-start", "co-publish-acked"),
    ("request: publish start -> Inventory handler start", "co-publish-start", "inv-handle-start"),
    ("Inventory db transaction", "inv-handle-start", "inv-db-committed"),
    ("Inventory reply send_and_wait (linger + broker ack)", "inv-db-committed", "inv-publish-acked"),
    ("Inventory mark outbox published (holds its slot)", "inv-publish-acked", "inv-handle-done"),
    ("reply: Inventory ack -> Checkout consumer sees it", "inv-publish-acked", "co-reply-dispatched"),
    ("reply: dispatched -> request resumes (event loop)", "co-reply-dispatched", "co-reply-resumed"),
    ("set state RESERVED (db tx)", "co-reply-resumed", "co-reserved-state"),
    ("finalize (db tx)", "co-reserved-state", "co-finalized"),
    ("receipt build", "co-finalized", "co-complete-done"),
    ("TOTAL inside Checkout saga", "co-complete-start", "co-complete-done"),
    ("TOTAL request-to-reply: publish start -> reply resumed", "co-publish-start", "co-reply-resumed"),
]


def pct(v: list[float], p: float) -> float:
    return v[min(len(v) - 1, int(len(v) * p))]


def row(name: str, v: list[float]) -> None:
    v = sorted(v)
    if not v:
        print(f"  {name:58} (no samples)")
        return
    print(f"  {name:58} n={len(v):6} mean={statistics.mean(v):7.2f} p50={pct(v, .5):7.2f} "
          f"p95={pct(v, .95):7.2f} p99={pct(v, .99):7.2f} ms")


def main(path: str) -> int:
    points: dict[str, dict[str, float]] = defaultdict(dict)   # tx -> point -> ts
    kafka_ts: dict[str, dict[str, float]] = defaultdict(dict)  # tx -> point -> kafka record ts
    batches: dict[tuple, list[tuple[str, float, int]]] = defaultdict(list)  # (service,pid,name)
    handle_starts: list[tuple[tuple, float, str]] = []
    slow = Counter()
    with open(path, errors="replace") as fh:
        for line in fh:
            m = LINE.search(line)
            if not m:
                continue
            try:
                d = json.loads(m.group(1))
            except ValueError:
                continue
            if d.get("step") == "publish" and d.get("outcome") in ("swept", "deferred"):
                slow[f"publish {d['outcome']} ({d.get('service')})"] += 1
            if d.get("step") in ("reserve", "release") and d.get("outcome") == "republished":
                slow[f"{d['step']} republished"] += 1
            if d.get("step") != "trace":
                continue
            point, tx, ts = d["outcome"], d.get("tx"), d["ts"]
            if point.startswith("consume-"):
                key = (d["service"], d["pid"], d.get("name"))
                batches[key].append((point, ts, d.get("n", 0)))
                continue
            if tx is None:
                continue
            points[tx].setdefault(point, ts)
            if d.get("kafka_ts"):
                kafka_ts[tx].setdefault(point, d["kafka_ts"])
            if point == "inv-handle-start":
                handle_starts.append(((d["service"], d["pid"], "inventory"), ts, tx))

    if not points:
        print("no trace lines found: run the stack with TRACE_TIMING=1")
        return 1
    print(f"completions traced: {sum(1 for p in points.values() if 'co-complete-done' in p)}"
          f" (of {len(points)} transactions with a trace)\n")
    print("Segments of one completion (ms):")
    for name, a, b in SEGMENTS:
        row(name, [p[b] - p[a] for p in points.values() if a in p and b in p and p[b] >= p[a]])
    row("Kafka record create -> Inventory handler start",
        [points[t]["inv-handle-start"] - k["inv-handle-start"] for t, k in kafka_ts.items()
         if "inv-handle-start" in k and "inv-handle-start" in points[t]])
    row("Kafka record create (Inventory reply) -> Checkout sees it",
        [points[t]["co-reply-dispatched"] - k["co-reply-dispatched"] for t, k in kafka_ts.items()
         if "co-reply-dispatched" in k and "co-reply-dispatched" in points[t]])

    print("\nConsumer loop (per poll batch):")
    for key in sorted(batches, key=str):
        ev = batches[key]
        sizes = [n for p, _, n in ev if p == "consume-polled"]
        handled, commit, idle = [], [], []
        last_poll = last_done = None
        for point, ts, _ in ev:
            if point == "consume-polled":
                if last_done is not None:
                    idle.append(ts - last_done)  # time between commit and the next batch arriving
                last_poll = ts
            elif point == "consume-batch-handled" and last_poll is not None:
                handled.append(ts - last_poll)
                last_handled = ts
            elif point == "consume-committed" and last_poll is not None:
                commit.append(ts - last_handled)
                last_done = ts
        if not sizes or not key[2]:
            continue
        print(f" {key[0]} pid {key[1]} consumer '{key[2]}': {len(sizes)} batches, "
              f"size mean={statistics.mean(sizes):.1f} max={max(sizes)}")
        row("   batch handling time (poll returned -> all handled)", handled)
        row("   offset commit", commit)
        row("   commit done -> next batch returned (idle/poll wait)", idle)

    waits = []
    last_poll_by = {}
    for key, ev in batches.items():
        last_poll_by[(key[0], key[1])] = sorted(ts for p, ts, _ in ev if p == "consume-polled")
    import bisect
    for (service, pid, _), ts, _tx in handle_starts:
        polls = last_poll_by.get((service, pid), [])
        i = bisect.bisect_right(polls, ts) - 1
        if i >= 0:
            waits.append(ts - polls[i])
    print("\nInventory wait inside a batch:")
    row("   poll returned -> this record's handler starts", waits)

    print("\nSlow paths taken:", dict(slow) or "none")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <compose log file>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
