from decimal import Decimal

import pytest

from marketflow.units import UnitError, normalize_price, parse_unit


@pytest.mark.parametrize("text,expected", [
    ("10kg", ("g", Decimal(10000))), ("100g", ("g", Decimal(100))), ("1.5L", ("ml", Decimal(1500))),
    ("500ml", ("ml", Decimal(500))), ("10개", ("unit", Decimal(10))), ("kg", ("g", Decimal(1000))),
    ("1,000g", ("g", Decimal(1000))),
])
def test_parse_unit(text, expected):
    assert parse_unit(text) == expected


@pytest.mark.parametrize("text", ["", "박스", "30m 30rolls", "abc"])
def test_unparseable_never_guessed(text):
    with pytest.raises(UnitError):
        parse_unit(text)


def test_normalize_price_half_up():
    assert normalize_price(Decimal("52300"), "10kg", "100g") == Decimal("523.00")
    assert normalize_price(Decimal("1450"), "1kg", "1kg") == Decimal("1450.00")
    assert normalize_price(Decimal("1000"), "3kg", "100g") == Decimal("33.33")


def test_dimension_mismatch():
    with pytest.raises(UnitError):
        normalize_price(Decimal("1000"), "10개", "100g")
