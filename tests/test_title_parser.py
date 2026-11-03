from decimal import Decimal

import pytest

from marketflow.title_parser import total_quantity, unit_price
from marketflow.units import UnitError

D = Decimal


@pytest.mark.parametrize("title,price,std,expected", [
    ("성주 사과 1.2kg(600g x 2)", 15900, "100g", "1325.00"),   # 15900 / 12
    ("[유기농] 성주참외 1.2kg (4-6입)", 12000, "100g", "1000.00"),
    ("양파 3kg", 4980, "100g", "166.00"),
    ("양파 3kg", 4980, "1kg", "1660.00"),
    ("깐마늘 500g", 6500, "100g", "1300.00"),
    ("깐마늘 500 g", 6500, "100g", "1300.00"),
    ("생수 500ml x 20", 8000, "100ml", "80.00"),
    ("생수 500ml×20병", 8000, "100ml", "80.00"),
    ("우유 1.5L", 3000, "100ml", "200.00"),
    ("우유 900ml*2", 5000, "100ml", "277.78"),
    ("휴지 30롤", 15000, "1unit", "500.00"),
    ("계란 30구", 7500, "1unit", "250.00"),
    ("두부 300g x 3팩", 4500, "100g", "500.00"),
    ("쌀 10kg", 29900, "1kg", "2990.00"),
    ("감자 1,000g", 3000, "100g", "300.00"),
])
def test_unit_price(title, price, std, expected):
    assert unit_price(title, price, std) == D(expected)


@pytest.mark.parametrize("title", ["30m 30rolls", "사과 선물세트 박스", "", "프리미엄 사과", "kg 사과", "대파 1단 500g"])  # last: ambiguous 1단 vs 500g -> not guessed
def test_unparseable_is_explicit_never_guessed(title):
    with pytest.raises(UnitError, match="UNPARSEABLE"):
        unit_price(title, 1000, "100g")


def test_multiplied_pack_wins_over_bare_quantity():
    assert total_quantity("사과 1.2kg(600g x 2)") == ("g", D(1200))
    assert total_quantity("물 2L 500ml x 6") == ("ml", D(3000))


def test_dimension_mismatch_is_unparseable():
    with pytest.raises(UnitError, match="UNPARSEABLE"):
        unit_price("휴지 30롤", 15000, "100g")
