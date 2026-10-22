"""Service boundaries (spec 007 FR-005, FR-007, FR-008, FR-022; contracts/service-boundaries.md).

The real tree must have zero violations, and each of the eleven rules must actually fire on a
minimal violating tree: a check that can't fail proves nothing.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("check_boundaries", ROOT / "scripts" / "check_boundaries.py")
check_boundaries = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_boundaries)
check = check_boundaries.check


def test_repository_has_no_boundary_violations():
    assert check(ROOT) == []


def write(root: pathlib.Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def svc(name: str, module: str = "x.py") -> str:
    return f"services/{name}/src/{name}_service/{module}"


# A compose file that satisfies every compose rule; the cases below break one thing each.
GOOD_COMPOSE = """
services:
  checkout-db: {networks: [checkout_data]}
  inventory-db: {networks: [inventory_data]}
  seed-catalog:
    volumes: ["catalog_data:/data"]
  seed-checkout:
    environment: {DATABASE_URL: "postgresql+asyncpg://checkout_user:checkout_pass@checkout-db:5432/checkout"}
    networks: [checkout_data]
  seed-inventory:
    environment: {DATABASE_URL: "postgresql+asyncpg://inventory_user:inventory_pass@inventory-db:5432/inventory"}
    networks: [inventory_data]
  catalog:
    volumes: ["catalog_data:/data:ro"]
    networks: [edge, mesh]
  checkout:
    environment: {DATABASE_URL: "postgresql+asyncpg://checkout_user:checkout_pass@checkout-db:5432/checkout"}
    networks: [edge, mesh, checkout_data]
  inventory:
    environment: {DATABASE_URL: "postgresql+asyncpg://inventory_user:inventory_pass@inventory-db:5432/inventory"}
    networks: [edge, mesh, inventory_data]
  analytics:
    environment: {WORKERS: "1"}
    networks: [edge, mesh]
"""

GOOD_NGINX = "http { server { location /internal/ { return 404 'x'; } } }"


def test_the_good_compose_fixture_is_clean(tmp_path):
    write(tmp_path, "compose.yaml", GOOD_COMPOSE)
    write(tmp_path, "gateway/nginx.conf", GOOD_NGINX)
    assert check(tmp_path) == []


PYTHON_CASES = {
    "rule 1": (svc("catalog"), "import checkout_service.service\n"),
    "rule 2 (catalog)": (svc("catalog"), "from sqlalchemy import text\n"),
    "rule 2 (analytics)": (svc("analytics"), "import asyncpg\n"),
    "rule 2 (checkout_web)": ("packages/checkout_web/src/checkout_web/x.py", "import sqlalchemy\n"),
    "rule 3 (import)": ("scripts/seed.py", "from checkout_data.seed import main\n"),
    "rule 5 (socket)": (svc("checkout"), "import socket\n"),
    "rule 5 (raw client)": (svc("checkout"), "import httpx\nc = httpx.AsyncClient()\n"),
    "rule 5 (unlisted peer)": (svc("analytics"), 'URL = "http://inventory:8000/internal/reservations"\n'),
}


@pytest.mark.parametrize("name", sorted(PYTHON_CASES))
def test_each_python_rule_fires(tmp_path: pathlib.Path, name: str):
    rel, text = PYTHON_CASES[name]
    write(tmp_path, rel, text)
    violations = check(tmp_path)
    assert any(v.startswith(name.split(" ")[0] + " " + name.split(" ")[1] + ":") for v in violations), violations


def test_rule_3_fires_when_the_directory_still_exists(tmp_path):
    (tmp_path / "packages" / "checkout_data").mkdir(parents=True)
    assert any(v.startswith("rule 3:") for v in check(tmp_path))


def test_rule_4_fires_on_another_services_table(tmp_path):
    write(tmp_path, svc("inventory", "models.py"), '__tablename__ = "inventory_stock"\n')
    write(tmp_path, svc("inventory", "repo.py"), 'Q = "SELECT sku FROM transaction_item WHERE id = 1"\n')
    assert any(v.startswith("rule 4:") and "transaction_item" in v for v in check(tmp_path))


def test_rule_4_allows_its_own_tables_and_cte_names(tmp_path):
    write(tmp_path, svc("inventory", "models.py"), '__tablename__ = "inventory_stock"\n')
    write(
        tmp_path,
        svc("inventory", "repo.py"),
        'Q = "WITH v AS (SELECT 1) SELECT s.sku FROM inventory_stock s JOIN v ON TRUE"\n',
    )
    assert check(tmp_path) == []


def mutate(old: str, new: str) -> str:
    assert old in GOOD_COMPOSE, old
    return GOOD_COMPOSE.replace(old, new)


COMPOSE_CASES = {
    "rule 6 (foreign db)": mutate(
        '{DATABASE_URL: "postgresql+asyncpg://checkout_user:checkout_pass@checkout-db:5432/checkout"}\n    networks: [edge, mesh, checkout_data]',
        '{DATABASE_URL: "postgresql+asyncpg://checkout_user:checkout_pass@inventory-db:5432/inventory"}\n    networks: [edge, mesh, checkout_data]',
    ),
    "rule 6 (analytics has a db)": mutate(
        'environment: {WORKERS: "1"}',
        'environment: {WORKERS: "1", DATABASE_URL: "postgresql+asyncpg://u:p@checkout-db:5432/c"}',
    ),
    "rule 7 (network)": mutate(
        "networks: [edge, mesh]\n  checkout:", "networks: [edge, mesh, checkout_data]\n  checkout:"
    ),
    "rule 7 (volume writable)": mutate('"catalog_data:/data:ro"', '"catalog_data:/data"'),
    "rule 7 (volume elsewhere)": mutate(
        'environment: {WORKERS: "1"}', 'environment: {WORKERS: "1"}\n    volumes: ["catalog_data:/data:ro"]'
    ),
    "rule 8": mutate(
        'environment: {WORKERS: "1"}', 'environment: {WORKERS: "1", X: "${INVENTORY_DB_PASSWORD}"}'
    ),
    "rule 11 (workers)": mutate('{WORKERS: "1"}', '{WORKERS: "2"}'),
    "rule 11 (replicas)": mutate(
        'environment: {WORKERS: "1"}', 'environment: {WORKERS: "1"}\n    deploy: {replicas: 2}'
    ),
}


@pytest.mark.parametrize("name", sorted(COMPOSE_CASES))
def test_each_compose_rule_fires(tmp_path: pathlib.Path, name: str):
    write(tmp_path, "compose.yaml", COMPOSE_CASES[name])
    rule = " ".join(name.split(" ")[:2])
    assert any(v.startswith(rule + ":") for v in check(tmp_path)), check(tmp_path)


@pytest.mark.parametrize(
    "conf",
    [
        "http { server { location /internal/ { proxy_pass http://checkout; } } }",
        "http { server { location / { proxy_pass http://checkout; } } }",
    ],
)
def test_rule_9_fires_when_internal_is_proxied_or_unblocked(tmp_path, conf):
    write(tmp_path, "gateway/nginx.conf", conf)
    assert any(v.startswith("rule 9:") for v in check(tmp_path))


def test_rule_10_fires_on_a_foreign_package_or_a_driver_in_a_lock(tmp_path):
    write(tmp_path, "services/catalog/requirements.lock", "orjson==3.9.10\nasyncpg==0.29.0\ncheckout-data==0.5.0\n")
    violations = [v for v in check(tmp_path) if v.startswith("rule 10:")]
    assert len(violations) == 2, violations
