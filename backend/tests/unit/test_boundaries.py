"""Service boundaries (spec FR-009, FR-011, FR-016; contracts/service-boundaries.md).

The real tree must have zero violations, and each of the seven rules must
actually fire on a minimal violating tree — a check that can't fail proves nothing.
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


SVC = "services/catalog/src/catalog_service/x.py"
REPO = "packages/checkout_data/src/checkout_data/repos/inventory.py"

CASES = {
    "rule 1": (SVC, "import checkout_service.service\n"),
    "rule 2": (SVC, "import httpx\n"),
    "rule 3": (SVC, "from checkout_data.repos import checkout as repo\n"),
    "rule 4": (SVC, "from sqlalchemy import text\n"),
    "rule 5": (REPO, 'Q = "UPDATE inventory_stock SET current_stock = 0"\n'),
    "rule 7": ("packages/checkout_data/src/checkout_data/bad.py", "import fastapi\n"),
}


@pytest.mark.parametrize("rule", sorted(CASES))
def test_each_rule_fires(tmp_path: pathlib.Path, rule: str):
    rel, text = CASES[rule]
    write(tmp_path, rel, text)
    violations = check(tmp_path)
    assert any(v.startswith(rule + ":") for v in violations), violations


def test_rule5_reads_need_a_declared_reader(tmp_path: pathlib.Path):
    write(tmp_path, REPO, 'Q = "SELECT id FROM transaction_item"\n')
    assert any(v.startswith("rule 5:") for v in check(tmp_path))


def test_rule5_allows_owned_writes_and_declared_reads(tmp_path: pathlib.Path):
    write(tmp_path, REPO, 'Q = "SELECT sku FROM inventory_stock s JOIN catalog_item c USING (sku)"\n')
    write(tmp_path, "packages/checkout_data/src/checkout_data/repos/analytics.py",
          'Q = "INSERT INTO popular_window_snapshot (id) VALUES (1)"\n')
    assert check(tmp_path) == []


def test_rule6_compose_env_must_not_name_another_service(tmp_path: pathlib.Path):
    write(tmp_path, "compose.yaml", (
        "services:\n"
        "  checkout:\n"
        "    environment:\n"
        "      DATABASE_URL: postgresql+asyncpg://checkout:checkout@postgres:5432/checkout\n"
        "      ANALYTICS_URL: http://analytics:8000\n"
    ))
    violations = check(tmp_path)
    assert any(v.startswith("rule 6:") and "analytics" in v for v in violations), violations


def test_rule6_database_url_with_checkout_user_is_not_a_hostname(tmp_path: pathlib.Path):
    write(tmp_path, "compose.yaml", (
        "services:\n"
        "  inventory:\n"
        "    environment:\n"
        "      DATABASE_URL: postgresql+asyncpg://checkout:checkout@postgres:5432/checkout\n"
    ))
    assert check(tmp_path) == []


def test_rule7_data_layer_must_not_raise_api_errors(tmp_path: pathlib.Path):
    write(tmp_path, "packages/checkout_data/src/checkout_data/bad.py", "def f():\n    raise ApiError(1, 'x')\n")
    assert any(v.startswith("rule 7:") for v in check(tmp_path))
