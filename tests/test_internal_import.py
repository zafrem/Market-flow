from datetime import date, datetime, timezone

import pytest
from openpyxl import Workbook
from sqlalchemy import text

from marketflow.db import init_sqlite, make_engine
from marketflow.internal_data import import_internal
from marketflow.ops_repo import OpsRepository
from marketflow.settings import Settings
from marketflow.time_provider import FixedTimeProvider

TP = FixedTimeProvider(datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc))   # 2026-11-03 KST
HEAD = "internal_sku,item_name,channel_name,purchase_cost,fixed_cost,logistics_cost,channel_fee_rate,current_selling_price,stock_qty,daily_sales_qty"
OK1 = "APPLE-1,Apple,SHOPA,5000,500,1000,11.5,10000,100,10"
OK2 = "ONION-1,Onion,SHOPA,2000,300,700,11.5,5000,300,100"


@pytest.fixture()
def repo(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'e.db'}")
    init_sqlite(e)
    r = OpsRepository(e)
    r.engine = e
    return r


def csv(tmp_path, *lines, enc="utf-8", name="inv.csv"):
    p = tmp_path / name
    p.write_bytes(("\n".join(lines) + "\n").encode(enc))
    return p


def inv(repo):
    with repo.engine.connect() as c:
        return c.execute(text("SELECT * FROM int_operations_inventory ORDER BY internal_sku")).mappings().all()


def test_valid_csv_loads_with_computed_floor_and_defaults(repo, tmp_path):
    res = import_internal(csv(tmp_path, HEAD, OK1, OK2), repo, Settings(), TP)
    assert (res.loaded, res.errors) == (2, [])
    a = inv(repo)[0]
    assert a["recorded_date"] == "2026-11-03" and a["min_price_floor"] == 8186      # break-even floor computed
    assert (a["promo_cost"], a["vat_inclusive"], float(a["pack_std_units"])) == (0, 1, 1.0)
    assert a["lifecycle_metric"] == '{"remaining_days": null, "holding_age_days": null}'


def test_explicit_floor_and_lifecycle_columns(repo, tmp_path):
    f = csv(tmp_path, HEAD + ",min_price_floor,remaining_days,holding_age_days,pack_std_units,vat_inclusive",
            OK1 + ",9000,5,2,12,N")
    assert import_internal(f, repo, Settings(), TP).loaded == 1
    a = inv(repo)[0]
    assert a["min_price_floor"] == 9000 and a["vat_inclusive"] == 0 and float(a["pack_std_units"]) == 12
    assert '"remaining_days": 5' in a["lifecycle_metric"]


def test_korean_headers_commas_won_and_percent_and_cp949(repo, tmp_path):
    f = csv(tmp_path, "상품코드,품목명,판매채널,매입원가,포장비,물류비,수수료율,현재판매가,재고수량,일판매량",
            'APPLE-1,Apple,SHOPA,"5,000원",500,1000,11.5%,"10,000",100,10', enc="cp949")
    res = import_internal(f, repo, Settings(), TP)
    assert (res.loaded, res.errors) == (1, []) and inv(repo)[0]["current_selling_price"] == 10000


def test_xlsx_with_real_date_cells(repo, tmp_path):
    wb = Workbook(); ws = wb.active
    ws.append(["date", *HEAD.split(",")])
    ws.append([datetime(2026, 11, 2), *OK1.split(",")])
    ws.append([None, "", "", "", "", "", "", "", "", "", ""])             # blank rows are ignored
    p = tmp_path / "inv.xlsx"; wb.save(p)
    res = import_internal(p, repo, Settings(), TP)
    assert (res.loaded, res.errors) == (1, []) and inv(repo)[0]["recorded_date"] == "2026-11-02"


def test_bad_rows_reported_with_line_numbers_while_good_rows_load(repo, tmp_path):
    f = csv(tmp_path, HEAD, OK1,
            "GHOST-1,Durian,SHOPA,5000,500,1000,11.5,10000,100,10",          # line 3 unknown item
            "ONION-1,Onion,SHOPA,abc,300,700,11.5,5000,300,100",             # line 4 non-numeric
            "ONION-2,Onion,SHOPA,2000,300,700,150,5000,300,100",             # line 5 fee > 100
            "ONION-3,Onion,SHOPA,2000,300,700,11.5,5000.5,300,100",          # line 6 fractional price
            "ONION-4,Onion,SHOPA,2000,300,700,11.5,5000,-1,100",             # line 7 negative stock
            "ONION-5,Onion,SHOPA,100,0,0,99,5000,3,1",                       # line 8 fee leaves no feasible floor
            OK1)                                                              # line 9 duplicate of line 2
    res = import_internal(f, repo, Settings(), TP)
    assert res.loaded == 1
    got = {e.line: e for e in res.errors}
    assert sorted(got) == [3, 4, 5, 6, 7, 8, 9]
    assert "master_products" in got[3].message and got[4].field == "purchase_cost" and "<= 100" in got[5].message
    assert "whole number" in got[6].message and ">= 0" in got[7].message and "no feasible" in got[8].message
    assert "duplicate" in got[9].message


def test_missing_required_column_loads_nothing(repo, tmp_path):
    f = csv(tmp_path, HEAD.replace("stock_qty,", ""), "APPLE-1,Apple,SHOPA,5000,500,1000,11.5,10000,10")
    res = import_internal(f, repo, Settings(), TP)
    assert res.loaded == 0 and [e.field for e in res.errors] == ["stock_qty"] and inv(repo) == []


def test_reimport_is_idempotent_and_updates(repo, tmp_path):
    import_internal(csv(tmp_path, HEAD, OK1), repo, Settings(), TP)
    import_internal(csv(tmp_path, HEAD, OK1.replace(",100,10", ",250,10")), repo, Settings(), TP)
    rows = inv(repo)
    assert len(rows) == 1 and rows[0]["stock_qty"] == 250


def test_same_sku_on_different_dates_keeps_history(repo, tmp_path):
    import_internal(csv(tmp_path, HEAD, OK1), repo, Settings(), TP, date(2026, 11, 1))
    import_internal(csv(tmp_path, HEAD, OK1.replace(",10000,100,10", ",10000,90,12")), repo, Settings(), TP, date(2026, 11, 2))
    assert repo.sales_history("APPLE-1", "SHOPA", date(2026, 11, 2)) == [12, 10]


def test_category_decision_override_changes_computed_floor(repo, tmp_path):
    s = Settings(decision={"Fresh": {"target_min_margin_pct": 10}})
    import_internal(csv(tmp_path, HEAD, OK1), repo, s, TP)
    assert inv(repo)[0]["min_price_floor"] > 8186


def test_unsupported_file_type(repo, tmp_path):
    p = tmp_path / "inv.txt"; p.write_text("x")
    with pytest.raises(ValueError, match="unsupported"):
        import_internal(p, repo, Settings(), TP)
