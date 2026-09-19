"""Unit tests: money math (T041)."""

from __future__ import annotations

import subprocess

import pytest

from app.money import catalog_price_cents, cents_to_amount


@pytest.mark.parametrize(
    "index,expected",
    [(1, 85), (2, 120), (3, 155), (46, 1660), (47, 50), (48, 85), (94, 50)],
)
def test_catalog_price_matches_the_mock_formula(index, expected):
    assert catalog_price_cents(index) == expected


def test_price_formula_wraps_every_47_items():
    for i in range(1, 200):
        assert catalog_price_cents(i) == catalog_price_cents(i + 47)


def test_cents_to_amount():
    assert cents_to_amount(0) == 0.0
    assert cents_to_amount(85) == 0.85
    assert cents_to_amount(120) == 1.20
    assert cents_to_amount(1234567) == 12345.67


def test_twenty_line_basket_total_does_not_drift():
    """The reason totals are summed in integer cents (research R4)."""
    basket = [catalog_price_cents(i) for i in range(1, 21)]

    exact = cents_to_amount(sum(basket))
    naive = sum(cents_to_amount(c) for c in basket)

    assert exact == pytest.approx(naive)
    # The integer sum is exact by construction; the float sum need not be.
    assert sum(basket) == int(round(exact * 100))


def test_large_total_stays_exact_in_cents():
    # 10_000 units of the most expensive item.
    cents = catalog_price_cents(46) * 10_000
    assert cents == 16_600_000
    assert cents_to_amount(cents) == 166000.0


def test_matches_java_mock_across_the_whole_catalog(tmp_path):
    """Cross-check against the actual Java expression, not a transcription of it."""
    src = tmp_path / "P.java"
    src.write_text(
        "public class P{public static void main(String[] a){"
        "StringBuilder s=new StringBuilder();"
        "for(int i=1;i<=2000;i++){double p=0.5+(i%47)*0.35;"
        "double r=Math.round(p*100.0)/100.0;"
        "s.append((int)Math.round(r*100)).append(i<2000?\",\":\"\");}"
        "System.out.println(s);}}"
    )
    try:
        subprocess.run(
            ["javac", str(src)], cwd=tmp_path, check=True, capture_output=True
        )
        out = subprocess.run(
            ["java", "-cp", str(tmp_path), "P"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("JDK not available")

    java = [int(x) for x in out.strip().split(",")]
    assert [catalog_price_cents(i) for i in range(1, 2001)] == java
