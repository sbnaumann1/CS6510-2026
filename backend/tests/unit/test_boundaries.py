"""Service boundaries (spec 007 FR-005, FR-007, FR-008, FR-022; spec 008 FR-002, FR-019;
contracts/service-boundaries.md).

The real tree must have zero violations, and each rule (the 007 rules and E1-E10) must
actually fire on a minimal violating tree: a check that can't fail proves nothing.
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
    networks: [edge_catalog, bus_catalog]
  checkout:
    environment: {DATABASE_URL: "postgresql+asyncpg://checkout_user:checkout_pass@checkout-db:5432/checkout"}
    networks: [edge_checkout, bus_checkout, checkout_data]
  inventory:
    environment: {DATABASE_URL: "postgresql+asyncpg://inventory_user:inventory_pass@inventory-db:5432/inventory"}
    networks: [edge_inventory, bus_inventory, inventory_data]
  analytics:
    environment: {WORKERS: "1"}
    networks: [edge_analytics, bus_analytics]
  kafka:
    networks: [bus_catalog, bus_checkout, bus_inventory, bus_analytics]
  gateway:
    networks: [edge_catalog, edge_checkout, edge_inventory, edge_analytics]
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
    "E2 (socket)": (svc("checkout"), "import socket\n"),
    "E2 (httpx)": (svc("checkout"), "import httpx\nc = httpx.AsyncClient()\n"),
    "E2 (requests)": (svc("inventory"), "import requests\n"),
    "E1 (peer address)": (svc("analytics"), 'URL = "http://inventory:8000/items"\n'),
    "E1 (any peer)": (svc("checkout"), 'URL = "http://catalog:8000"\n'),
    "E8 (internal route)": (svc("inventory"), 'ROUTE = router.post("/internal/reservations")\n'),
    "E10 (checkout_web knows a service)": ("packages/checkout_web/src/checkout_web/x.py", "import checkout_service\n"),
}


@pytest.mark.parametrize("name", sorted(PYTHON_CASES))
def test_each_python_rule_fires(tmp_path: pathlib.Path, name: str):
    rel, text = PYTHON_CASES[name]
    write(tmp_path, rel, text)
    violations = check(tmp_path)
    rule = name.split(" ")[0] if name.startswith("E") else " ".join(name.split(" ")[:2])
    assert any(v.startswith(rule + ":") for v in violations), violations


def test_internal_reports_are_allowed(tmp_path):
    write(tmp_path, svc("checkout"), 'ROUTE = router.get("/internal/reports/scan-count")\n')
    assert check(tmp_path) == []


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
        '{DATABASE_URL: "postgresql+asyncpg://checkout_user:checkout_pass@checkout-db:5432/checkout"}\n    networks: [edge_checkout, bus_checkout, checkout_data]',
        '{DATABASE_URL: "postgresql+asyncpg://checkout_user:checkout_pass@inventory-db:5432/inventory"}\n    networks: [edge_checkout, bus_checkout, checkout_data]',
    ),
    "rule 6 (analytics has a db)": mutate(
        'environment: {WORKERS: "1"}',
        'environment: {WORKERS: "1", DATABASE_URL: "postgresql+asyncpg://u:p@checkout-db:5432/c"}',
    ),
    "rule 7 (network)": mutate(
        "networks: [edge_catalog, bus_catalog]\n  checkout:",
        "networks: [edge_catalog, bus_catalog, checkout_data]\n  checkout:",
    ),
    "E7 (two services, one network)": mutate(
        "networks: [edge_analytics, bus_analytics]", "networks: [edge_analytics, bus_analytics, bus_catalog]"
    ),
    "E1 (compose peer address)": mutate(
        'environment: {WORKERS: "1"}', 'environment: {WORKERS: "1", CHECKOUT_URL: "http://checkout:8000"}'
    ),
    "E1 (compose peer variable)": mutate(
        'environment: {WORKERS: "1"}', 'environment: {WORKERS: "1", INVENTORY_URL: "svc"}'
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
    rule = name.split(" ")[0] if name.startswith("E") else " ".join(name.split(" ")[:2])
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


# ---- E2 in the manifests ------------------------------------------------------------------


def test_e2_fires_on_an_http_client_dependency(tmp_path):
    write(tmp_path, "services/checkout/pyproject.toml", 'dependencies = [\n    "orjson==3.9.10",\n    "httpx==0.25.2",\n]\n')
    assert any(v.startswith("E2:") and "httpx" in v for v in check(tmp_path))
    write(tmp_path, "services/catalog/requirements.lock", "orjson==3.9.10\nrequests==2.31.0\n")
    assert any(v.startswith("E2:") and "requirements.lock" in v for v in check(tmp_path))


# ---- the event topology: E3-E6 and E9 -----------------------------------------------------

EVENTS_PY = """
CATALOG_ITEMS = "catalog.items.v1"
CHECKOUT_SCANS = "checkout.scans.v1"
DEAD_LETTERS = "dead-letters.v1"
ITEM_PUBLISHED = "catalog.item-published"
SCAN_RECORDED = "checkout.scan-recorded"
DEAD_LETTER = "dead-letter"
"""

GOOD_TOPICS = """
topics:
  catalog.items.v1: {partitions: 1, compact: true, producer: catalog, consumers: [checkout]}
  checkout.scans.v1: {partitions: 1, compact: false, producer: checkout, consumers: []}
  dead-letters.v1: {partitions: 1, compact: false, producer: "*", consumers: []}
"""

SERVICE_EVENTS = {
    "catalog": "PRODUCES = (CATALOG_ITEMS,)\nCONSUMES = ()\n",
    "checkout": "PRODUCES = (CHECKOUT_SCANS,)\nCONSUMES = (CATALOG_ITEMS,)\n",
}


def events_tree(root: pathlib.Path, *, topics: str = GOOD_TOPICS, events: dict | None = None) -> None:
    write(root, "events/topics.yaml", topics)
    write(root, "packages/checkout_web/src/checkout_web/events.py", EVENTS_PY)
    for name in ("envelope", "catalog.item-published", "checkout.scan-recorded", "dead-letter"):
        write(root, f"events/schemas/{name}.json", "{}")
    for name, text in (events or SERVICE_EVENTS).items():
        write(root, svc(name, "events.py"), text)


def test_a_good_event_topology_is_clean(tmp_path):
    events_tree(tmp_path)
    write(tmp_path, svc("catalog", "publisher.py"), "from checkout_web.events import CATALOG_ITEMS\nx = CATALOG_ITEMS\n")
    assert check(tmp_path) == []


def test_e3_fires_on_a_topic_a_service_did_not_declare(tmp_path):
    events_tree(tmp_path)
    write(tmp_path, svc("catalog", "x.py"), "from checkout_web.events import CHECKOUT_SCANS\nx = CHECKOUT_SCANS\n")
    assert any(v.startswith("E3:") and "checkout.scans.v1" in v for v in check(tmp_path))


def test_e3_fires_on_a_topic_name_written_as_a_literal(tmp_path):
    events_tree(tmp_path)
    write(tmp_path, svc("catalog", "x.py"), 'TOPIC = "catalog.items.v1"\n')
    assert any(v.startswith("E3:") and "literal" in v for v in check(tmp_path))


def test_e3_fires_on_a_declared_name_that_is_not_a_topic(tmp_path):
    events_tree(tmp_path, events={**SERVICE_EVENTS, "catalog": "PRODUCES = (CATALOG_ITEMS, NOT_A_TOPIC)\nCONSUMES = ()\n"})
    assert any(v.startswith("E3:") and "NOT_A_TOPIC" in v for v in check(tmp_path))


def test_e4_fires_when_two_services_produce_one_topic(tmp_path):
    events_tree(tmp_path, events={**SERVICE_EVENTS, "checkout": "PRODUCES = (CHECKOUT_SCANS, CATALOG_ITEMS)\nCONSUMES = ()\n"})
    assert any(v.startswith("E4:") and "catalog.items.v1" in v for v in check(tmp_path))


def test_e4_fires_when_the_owner_does_not_produce_its_topic(tmp_path):
    events_tree(tmp_path, events={**SERVICE_EVENTS, "catalog": "PRODUCES = ()\nCONSUMES = ()\n"})
    assert any(v.startswith("E4:") and "catalog.items.v1" in v for v in check(tmp_path))


def test_e5_fires_on_a_consumer_of_a_topic_with_no_producer(tmp_path):
    topics = GOOD_TOPICS.replace("producer: catalog, ", "")
    events_tree(tmp_path, topics=topics)
    assert any(v.startswith("E5:") and "catalog.items.v1" in v for v in check(tmp_path))


def test_e5_fires_when_the_manifest_does_not_list_the_consumer(tmp_path):
    events_tree(tmp_path, topics=GOOD_TOPICS.replace("consumers: [checkout]", "consumers: []"))
    assert any(v.startswith("E5:") and "checkout consumes catalog.items.v1" in v for v in check(tmp_path))


def test_e6_fires_when_a_service_creates_another_services_event(tmp_path):
    events_tree(tmp_path)
    write(
        tmp_path,
        svc("checkout", "x.py"),
        "from checkout_web.events import ITEM_PUBLISHED, make_event\nmake_event(ITEM_PUBLISHED, 'checkout', {})\n",
    )
    assert any(v.startswith("E6:") and "catalog.item-published" in v for v in check(tmp_path))


def test_e6_allows_the_owner_and_dead_letters(tmp_path):
    events_tree(tmp_path)
    write(
        tmp_path,
        svc("checkout", "x.py"),
        "from checkout_web.events import SCAN_RECORDED, DEAD_LETTER, make_event\n"
        "make_event(SCAN_RECORDED, 'checkout', {})\nmake_event(DEAD_LETTER, 'checkout', {})\n",
    )
    assert check(tmp_path) == []


def test_e9_fires_on_an_event_type_without_a_schema(tmp_path):
    events_tree(tmp_path)
    (tmp_path / "events/schemas/checkout.scan-recorded.json").unlink()
    assert any(v.startswith("E9:") and "checkout.scan-recorded" in v for v in check(tmp_path))
