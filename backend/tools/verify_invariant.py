#!/usr/bin/env python
"""The graded correctness check across all stores (spec 007 FR-024, SC-002, research R10).

    python tools/verify_invariant.py          # host: services on their debug ports
    docker compose run --rm tools python -m verify_invariant      # inside the stack

It opens no database. Each service describes its own data over the mesh, and the checks
from data-model.md §6 run over those reports:

  1. per SKU, `initial_stock - current_stock` equals the units on COMPLETED transactions
  2. no stock is negative
  3. no transaction is COMPLETING and no completion is in a non-terminal state
  4. every APPLIED reservation belongs to a COMPLETED transaction and vice versa
  5. Analytics has read every scan Checkout holds (waited for, up to 30 s)

Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

CHECKOUT_URL = os.getenv("CHECKOUT_URL", "http://localhost:8082")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8083")
ANALYTICS_URL = os.getenv("ANALYTICS_URL", "http://localhost:8084")
CATCH_UP_S = float(os.getenv("VERIFY_CATCH_UP_S", "30"))


def check(checkout: dict, scan_count: dict, inventory: dict, analytics: dict) -> tuple[bool, list[str]]:
    """Run the invariants over the services' reports. Returns (ok, report lines)."""
    lines: list[str] = []
    sold: dict[str, int] = checkout["sold"]
    stock = inventory["stock"]

    mismatched = [
        r for r in stock if r["initial"] - r["current"] != sold.get(r["sku"], 0)
    ]
    negative = [r for r in stock if r["current"] < 0]
    unknown = sorted(set(sold) - {r["sku"] for r in stock})
    decremented = sum(r["initial"] - r["current"] for r in stock)

    lines.append(f"SKUs checked:          {len(stock)}")
    lines.append(f"Units decremented:     {decremented}")
    lines.append(f"Completed line items:  {sum(sold.values())}")
    lines.append(f"Negative stock SKUs:   {len(negative)}")
    lines.append(f"Mismatched SKUs:       {len(mismatched)}")

    ok = True
    if not stock:
        lines.append("FAIL: Inventory reports no stock (was it seeded?)")
        ok = False
    for r in sorted(mismatched, key=lambda r: abs(r["initial"] - r["current"] - sold.get(r["sku"], 0)), reverse=True)[:10]:
        drift = r["initial"] - r["current"] - sold.get(r["sku"], 0)
        lines.append(
            f"  {r['sku']}: initial={r['initial']} current={r['current']} "
            f"sold={sold.get(r['sku'], 0)} drift={drift:+d}"
        )
    for r in negative[:10]:
        lines.append(f"  {r['sku']}: NEGATIVE STOCK {r['current']}")
    if unknown:
        lines.append(f"  sold SKUs unknown to Inventory: {unknown[:10]}")
    ok = ok and not mismatched and not negative and not unknown

    if checkout["completing"] or checkout["openCompletions"]:
        lines.append(
            f"FAIL: {checkout['completing']} transaction(s) COMPLETING, "
            f"{checkout['openCompletions']} completion(s) not finished"
        )
        ok = False

    applied = set(inventory["adjustments"]["applied"])
    completed = set(checkout["completedTransactions"])
    if applied != completed:
        lines.append(
            f"FAIL: APPLIED reservations without a COMPLETED transaction: {sorted(applied - completed)[:10]}; "
            f"COMPLETED transactions without a reservation: {sorted(completed - applied)[:10]}"
        )
        ok = False

    if analytics["lastScanId"] != scan_count["maxScanId"]:
        lines.append(
            f"FAIL: Analytics has read to scan {analytics['lastScanId']}, "
            f"Checkout holds {scan_count['maxScanId']}"
        )
        ok = False

    lines.append(
        "\nPASS — initial_stock - current_stock == completed line items, per SKU, and every store agrees"
        if ok
        else "\nFAIL — see above"
    )
    return ok, lines


def fetch(client: httpx.Client) -> tuple[dict, dict, dict, dict]:
    def get(base: str, path: str) -> dict:
        r = client.get(base + path)
        r.raise_for_status()
        return r.json()

    return (
        get(CHECKOUT_URL, "/internal/reports/sold-by-sku"),
        get(CHECKOUT_URL, "/internal/reports/scan-count"),
        get(INVENTORY_URL, "/internal/reports/stock"),
        get(ANALYTICS_URL, "/internal/reports/cursor"),
    )


def main() -> int:
    with httpx.Client(timeout=30) as client:
        deadline = time.monotonic() + CATCH_UP_S
        while True:
            try:
                reports = fetch(client)
            except httpx.HTTPError as exc:
                print(f"FAIL: cannot reach a service: {exc}")
                return 1
            # Analytics reads scans asynchronously: give it time to catch up before judging.
            if reports[3]["lastScanId"] >= reports[1]["maxScanId"] or time.monotonic() > deadline:
                break
            time.sleep(0.25)
    ok, lines = check(*reports)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
