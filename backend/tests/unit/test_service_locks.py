"""Each image's committed requirements.lock must match the workspace uv.lock.

The Dockerfiles install these files, not uv.lock, so a stale one would ship a
different dependency set than the tests ran against. Regenerate with
scripts/lock_services.sh (005 research R3).
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

LOCKS = {
    "catalog-service": "services/catalog/requirements.lock",
    "checkout-service": "services/checkout/requirements.lock",
    "inventory-service": "services/inventory/requirements.lock",
    "analytics-service": "services/analytics/requirements.lock",
}


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
@pytest.mark.parametrize("package,lock", sorted(LOCKS.items()))
def test_lock_file_is_fresh(package: str, lock: str, tmp_path: pathlib.Path):
    out = tmp_path / "req.lock"
    subprocess.run(
        [
            "uv", "export", "--frozen", "--no-dev", "--no-emit-workspace",
            "--no-hashes", "--no-header", "--package", package, "-o", str(out),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    assert out.read_text() == (ROOT / lock).read_text(), (
        f"{lock} is stale; run scripts/lock_services.sh"
    )


def _names(lock: str) -> set[str]:
    return {
        line.split("==")[0].split("[")[0].strip().lower()
        for line in (ROOT / lock).read_text().splitlines()
        if line and not line.startswith(("#", " ", "-"))
    }


@pytest.mark.parametrize("package,lock", sorted(LOCKS.items()))
def test_a_lock_holds_no_other_services_package(package: str, lock: str):
    """Boundary rule 10: each image's dependency set is its own."""
    names = _names(lock)
    others = {p.replace("-", "_").lower() for p in LOCKS if p != package}
    assert not (names & others | names & {p.lower() for p in LOCKS if p != package})
    assert "checkout-data" not in names and "checkout_data" not in names


@pytest.mark.parametrize("service", ["catalog", "analytics"])
def test_services_without_a_database_ship_no_database_driver(service: str):
    """Catalog keeps a file and Analytics keeps nothing: no driver in their images."""
    names = _names(f"services/{service}/requirements.lock")
    assert not names & {"sqlalchemy", "asyncpg", "psycopg2-binary", "psycopg2", "psycopg"}
