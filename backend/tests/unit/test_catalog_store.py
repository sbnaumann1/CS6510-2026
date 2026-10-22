"""Catalog's file store and its seed (data-model.md §1, research R8-R9)."""

from __future__ import annotations

import pytest

from catalog_service import seed, store
from catalog_service.store import CatalogFileError


def write(path, *lines: str) -> str:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_round_trip_with_the_seed(tmp_path):
    path = tmp_path / "catalog.csv"
    assert seed.write(path, 120) == 120
    assert store.load(path) == 120
    rows = store.rows()
    assert rows[0] == ("SKU-000001", "Item 1", 85)
    assert rows[-1][0] == "SKU-000120"
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)


def test_header_must_match(tmp_path):
    p = write(tmp_path / "c.csv", "sku,name,price", "SKU-1,A,10")
    with pytest.raises(CatalogFileError, match="header"):
        store.load(p)


@pytest.mark.parametrize(
    "row,message",
    [
        (",A,10", "non-empty"),
        ("SKU-1,,10", "non-empty"),
        ("SKU-1,A,abc", "not an integer"),
        ("SKU-1,A,0", "> 0"),
        ("SKU-1,A,-5", "> 0"),
        ("SKU-1,A", "3 columns"),
    ],
)
def test_bad_rows_stop_startup_with_the_line_number(tmp_path, row, message):
    p = write(tmp_path / "c.csv", "sku,name,price_cents", row)
    with pytest.raises(CatalogFileError, match=message) as err:
        store.load(p)
    assert ":2:" in str(err.value)


def test_duplicate_and_unordered_skus_are_rejected(tmp_path):
    dup = write(tmp_path / "d.csv", "sku,name,price_cents", "SKU-1,A,10", "SKU-1,B,20")
    with pytest.raises(CatalogFileError, match="duplicate"):
        store.load(dup)
    unordered = write(tmp_path / "u.csv", "sku,name,price_cents", "SKU-2,A,10", "SKU-1,B,20")
    with pytest.raises(CatalogFileError, match="ascending"):
        store.load(unordered)


def test_missing_file_is_an_error_not_an_empty_catalog(tmp_path):
    with pytest.raises(CatalogFileError, match="cannot open"):
        store.load(tmp_path / "absent.csv")


def test_seed_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "catalog.csv"
    seed.write(path, 10)
    first = path.read_text()
    seed.write(path, 10)
    assert path.read_text() == first
    assert [p.name for p in tmp_path.iterdir()] == ["catalog.csv"]


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "catalog.csv"
    seed.write(path, 10)
    before = path.read_text()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(seed.os, "replace", boom)
    with pytest.raises(OSError):
        seed.write(path, 20)
    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["catalog.csv"]


def test_if_empty_does_nothing_when_the_file_has_items(tmp_path, monkeypatch):
    path = tmp_path / "catalog.csv"
    monkeypatch.setattr(seed, "settings", type("S", (), {"catalog_file": str(path), "catalog_size": 5})())
    assert seed.main(["--if-empty"]) == 0
    assert seed.has_items(path)
    before = path.read_text()
    path.write_text(before + "SKU-999999,Extra,1\n")
    assert seed.main(["--if-empty"]) == 0
    assert "SKU-999999" in path.read_text(), "an existing catalog is not overwritten"
    assert seed.main(["--reset"]) == 0
    assert "SKU-999999" not in path.read_text()
