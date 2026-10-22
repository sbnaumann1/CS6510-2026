"""ScanWindow: Analytics' in-memory rolling window (data-model.md §4)."""

from __future__ import annotations

from analytics_service.scan_window import ScanWindow


def fill(window: ScanWindow, skus: list[str], first_id: int = 1) -> None:
    for offset, sku in enumerate(skus):
        window.append(first_id + offset, sku)


def test_empty_window_has_zero_bounds_and_no_counts():
    w = ScanWindow(1000, 500)
    assert w.bounds() == (0, 0)
    assert w.counts(200) == []


def test_fewer_scans_than_a_window_starts_at_zero():
    w = ScanWindow(1000, 500)
    fill(w, ["A", "A", "B"])
    assert w.bounds() == (0, 3)
    assert w.counts(10) == [("A", 2), ("B", 1)]


def test_window_slides_past_old_scans():
    w = ScanWindow(10, 5)
    fill(w, ["OLD"] * 10 + ["NEW"] * 10)  # ids 1..20
    assert w.bounds() == (10, 20)
    assert w.counts(10) == [("NEW", 10)]


def test_ties_break_by_sku_and_depth_truncates():
    w = ScanWindow(100, 50)
    fill(w, ["C", "B", "A", "C", "B", "A", "D"])
    assert w.counts(3) == [("A", 2), ("B", 2), ("C", 2)]


def test_memory_is_bounded_to_a_window_plus_one_slide():
    w = ScanWindow(10, 5)
    fill(w, ["A"] * 100)
    assert len(w) == 15
    assert w.bounds() == (90, 100)


def test_clear_forgets_everything():
    w = ScanWindow(10, 5)
    fill(w, ["A"] * 3)
    w.clear()
    assert (len(w), w.bounds()) == (0, (0, 0))
