#!/usr/bin/env python
"""Hot-SKU contention probe (spec 008): is completion latency dominated by Inventory's
FOR UPDATE row locks when many shoppers buy the same SKU?

    python scripts/hot_sku_load.py --hot-fraction 0.5 --stations 50 --duration 30

Each station loops: start a transaction, scan 1-5 items, complete it. Every scan picks one
of `--hot-skus` hot SKUs with probability `--hot-fraction`, else a uniform random SKU, so
0.0 is the no-contention baseline and 1.0 puts the hot SKU in every basket. It prints the
latency percentiles of each operation and the status counts, and, while it runs, samples
`pg_stat_activity` in inventory-db once per `--sample-interval` seconds, reporting how many
backends were waiting on a lock. Run it through scripts/run_hot_sku_test.sh.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
from collections import Counter, defaultdict

import httpx

PSQL = ["docker", "compose", "exec", "-T", "inventory-db", "psql", "-U", "inventory_user",
        "-d", "inventory", "-Atc",
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = 'inventory' AND wait_event_type = 'Lock'"]


async def station(n: int, client: httpx.AsyncClient, skus: list[str], hot: list[str],
                  args, deadline: float, lat: dict, status: Counter) -> None:
    rng = random.Random(n)
    while time.monotonic() < deadline:
        async def call(op: str, method: str, url: str, **kw) -> httpx.Response:
            t0 = time.perf_counter()
            r = await client.request(method, url, **kw)
            lat[op].append((time.perf_counter() - t0) * 1000)
            status[(op, r.status_code)] += 1
            return r

        r = await call("start", "POST", "/transactions", json={"stationId": f"hot-{n}"})
        if r.status_code != 201:
            continue
        tx = r.json()["transactionId"]
        ok = True
        for _ in range(rng.randint(1, 5)):
            sku = rng.choice(hot) if rng.random() < args.hot_fraction else rng.choice(skus)
            r = await call("scan", "POST", f"/transactions/{tx}/items", json={"sku": sku})
            ok = ok and r.status_code == 200
        if ok:
            await call("complete", "POST", f"/transactions/{tx}/complete", json={})


async def sample_lock_waits(stop: asyncio.Event, interval: float, out: list[int]) -> None:
    while not stop.is_set():
        proc = await asyncio.create_subprocess_exec(
            *PSQL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        text, _ = await proc.communicate()
        try:
            out.append(int(text.strip()))
        except ValueError:
            pass
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            pass


def pct(values: list[float], p: float) -> float:
    return values[min(len(values) - 1, int(len(values) * p))]


async def main(args) -> int:
    limits = httpx.Limits(max_connections=args.stations * 2)
    async with httpx.AsyncClient(base_url=args.url, timeout=10, limits=limits) as client:
        items = (await client.get("/items")).json()["items"]
        skus = [i["sku"] for i in items]
        hot = skus[: args.hot_skus]
        lat: dict[str, list[float]] = defaultdict(list)
        status: Counter = Counter()
        waits: list[int] = []
        stop = asyncio.Event()
        sampler = asyncio.create_task(sample_lock_waits(stop, args.sample_interval, waits))
        started = time.monotonic()
        deadline = started + args.duration
        await asyncio.gather(*(station(n, client, skus, hot, args, deadline, lat, status)
                               for n in range(args.stations)))
        elapsed = time.monotonic() - started
        stop.set()
        await sampler

    print(f"\nhot-fraction={args.hot_fraction} hot-skus={args.hot_skus} "
          f"stations={args.stations} duration={elapsed:.0f}s "
          f"-> {len(lat['complete']) / elapsed:.1f} completions/s")
    for op in ("start", "scan", "complete"):
        v = sorted(lat[op])
        if v:
            print(f"  {op:9} n={len(v):6} mean={statistics.mean(v):7.1f} p50={pct(v, .5):7.1f} "
                  f"p95={pct(v, .95):7.1f} p99={pct(v, .99):7.1f} max={v[-1]:7.1f} ms")
    print("  status:", ", ".join(f"{op}:{code}x{n}" for (op, code), n in sorted(status.items())))
    if waits:
        print(f"  inventory-db backends waiting on a lock ({len(waits)} samples): "
              f"mean={statistics.mean(waits):.2f} max={max(waits)} "
              f"nonzero in {sum(1 for w in waits if w) / len(waits):.0%} of samples")
    else:
        print("  lock-wait sampling returned nothing (is inventory-db up and run from backend/?)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--stations", type=int, default=50)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--hot-fraction", type=float, default=0.5,
                    help="probability that a scan picks a hot SKU (0 = uniform baseline)")
    ap.add_argument("--hot-skus", type=int, default=1, help="how many SKUs are hot")
    ap.add_argument("--sample-interval", type=float, default=0.25)
    raise SystemExit(asyncio.run(main(ap.parse_args())))
