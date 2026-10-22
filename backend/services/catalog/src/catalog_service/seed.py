"""Seed the catalog file (spec 007 research R9).

    python -m catalog_service.seed --if-empty   # compose `seed-catalog` job
    python -m catalog_service.seed --reset      # rewrite the file
    python -m catalog_service.seed              # same as --reset (the file is the whole state)

Writes CATALOG_SIZE items using the mock server's exact SKU, name and price formulas.
The file is written to a temporary name in the same directory and renamed over the
target, so a reader never sees a partial file. `--if-empty` does nothing when the file
already has at least one data row.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import tempfile
from pathlib import Path

from catalog_service.config import settings
from catalog_service.store import HEADER


# Catalog price formula, byte-compatible with mockserver/MockServer.java:
#     double price = 0.5 + (i % 47) * 0.35;
#     Math.round(price * 100.0) / 100.0
# Java's Math.round(double) is floor(x + 0.5), NOT Python's banker's rounding.
def catalog_price_cents(index: int) -> int:
    """Price in cents for 1-based catalog index `index`."""
    price = 0.5 + (index % 47) * 0.35
    return math.floor(price * 100.0 + 0.5)


def has_items(path: Path) -> bool:
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader, None)
            return next(reader, None) is not None
    except OSError:
        return False


def write(path: Path, size: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".catalog-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(HEADER)
            for i in range(1, size + 1):
                writer.writerow([f"SKU-{i:06d}", f"Item {i}", catalog_price_cents(i)])
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return size


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--reset", action="store_true", help="rewrite the file")
    mode.add_argument(
        "--if-empty", action="store_true", help="write only if the file is absent or has no items"
    )
    args = ap.parse_args(argv)

    path = Path(settings.catalog_file)
    if args.if_empty and has_items(path):
        print(f"catalog file {path} already has items; skipping")
        return 0
    n = write(path, settings.catalog_size)
    print(f"wrote {n} catalog items to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
