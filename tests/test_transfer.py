"""Whole-data export and import: one zip, round trip into a fresh database, nothing half-loaded, ADMIN only."""
import io
import json
import zipfile

import pytest
from sqlalchemy import text

from marketflow import demo, transfer
from marketflow.db import init_sqlite, make_engine
from marketflow.settings import Settings
from test_api import login, world  # noqa: F401  (fixture)
from test_demo import loaded  # noqa: F401  (fixture: a database with demo data)


def _zip(entries: dict[str, bytes]) -> io.BytesIO:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in entries.items():
            z.writestr(k, v)
    b.seek(0)
    return b


def _count(e, sql):
    with e.connect() as c:
        return c.execute(text(sql)).scalar()


def test_round_trip_into_a_fresh_database(loaded, tmp_path):
    src, _ = loaded
    out = io.BytesIO()
    manifest = transfer.export_all(src, Settings().inbox_dir, out)
    assert manifest["tables"]["ext_retail_prices"] > 0 and manifest["images"]
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as z:
        assert "users" not in " ".join(z.namelist()) and "audit_log" not in " ".join(z.namelist())     # accounts stay put
    dst = make_engine(f"sqlite:///{tmp_path / 'b.db'}"); init_sqlite(dst)
    got = transfer.import_all(dst, str(tmp_path / "inbox"), io.BytesIO(out.getvalue()))
    assert got["images"] == len(manifest["images"]) and got["flyers"] == manifest["tables"]["flyers"]
    for t, n in manifest["tables"].items():
        if t not in ("master_products", "master_product_aliases", "mi_synonyms"):
            assert _count(dst, f"SELECT COUNT(*) FROM {t}") == n, t
    for sql in ("SELECT SUM(sale_price) FROM ext_retail_prices", "SELECT SUM(value) FROM mi_observations",
                "SELECT SUM(suggested_price) FROM price_recommendations", "SELECT SUM(purchase_cost) FROM int_operations_inventory"):
        assert _count(dst, sql) == _count(src, sql), sql
    # links survive the re-numbering: every retail row and flyer item still finds its parent
    assert _count(dst, "SELECT COUNT(*) FROM ext_retail_prices r JOIN master_products m ON m.std_item_id = r.std_item_id") == manifest["tables"]["ext_retail_prices"]
    assert _count(dst, "SELECT COUNT(*) FROM flyer_items i JOIN flyers f ON f.flyer_id = i.flyer_id") == manifest["tables"]["flyer_items"]
    assert (tmp_path / "inbox" / "flyer" / "review" / manifest["images"][0]).is_file()


def test_a_bad_file_changes_nothing(loaded, tmp_path):
    src, _ = loaded
    out = io.BytesIO(); transfer.export_all(src, Settings().inbox_dir, out)
    z = zipfile.ZipFile(io.BytesIO(out.getvalue()))
    entries = {n: z.read(n) for n in z.namelist()}
    entries["data/promotions.jsonl"] = b'{"promo_id": 1, "no_such_column": 3}\n'
    dst = make_engine(f"sqlite:///{tmp_path / 'c.db'}"); init_sqlite(dst)
    with pytest.raises(transfer.TransferError, match="unknown column"):
        transfer.import_all(dst, str(tmp_path / "inbox"), _zip(entries))
    assert _count(dst, "SELECT COUNT(*) FROM ext_retail_prices") == 0 and _count(dst, "SELECT COUNT(*) FROM master_products") == 8
    assert not (tmp_path / "inbox" / "flyer").exists()


@pytest.mark.parametrize("entries,msg", [
    ({"../evil.txt": b"x", "manifest.json": b"{}"}, "unexpected entry"),
    ({"inbox/flyer/review/../../x.png": b"x"}, "unexpected entry"),
    ({"data/users.jsonl": b""}, "unexpected entry"),
    ({"data/promotions.jsonl": b""}, "manifest.json is missing"),
    ({"manifest.json": json.dumps({"format": 99}).encode()}, "unsupported export format"),
])
def test_unsafe_or_foreign_archives_are_refused(entries, msg, tmp_path):
    dst = make_engine(f"sqlite:///{tmp_path / 'd.db'}"); init_sqlite(dst)
    with pytest.raises(transfer.TransferError, match=msg):
        transfer.import_all(dst, str(tmp_path / "inbox"), _zip(entries))
    with pytest.raises(transfer.TransferError, match="not a zip"):
        transfer.import_all(dst, str(tmp_path / "inbox"), io.BytesIO(b"hello"))


# ---- API ---------------------------------------------------------------------------------------------------
def test_export_and_import_are_admin_only(world):
    for who in ("md_fruit", "buyer"):
        h = login(world, who)
        assert world.client.get("/admin/export", headers=h).status_code == 403
        assert world.client.post("/admin/import", headers=h, content=b"x").status_code == 403


def test_export_then_clear_then_import_restores_the_data(world):
    h = login(world, "admin")
    r = world.client.get("/admin/export", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and "attachment" in r.headers["content-disposition"]
    blob = r.content
    before = demo.counts(world.e)
    assert world.client.post("/admin/import", headers=h, content=blob).status_code == 409        # data is there: clear first
    assert world.client.post("/admin/import", headers=h, content=b"not a zip").status_code in (409, 422)
    world.client.post("/admin/clear", headers=h, json={"confirm": "DELETE"})
    assert world.client.post("/admin/import", headers=h, content=b"not a zip").status_code == 422
    r = world.client.post("/admin/import", headers=h, content=blob)
    assert r.status_code == 200, r.text
    after = demo.counts(world.e)
    for t in demo.OPERATIONAL_TABLES:
        assert after[t] == before[t], t
    assert after["users"] == before["users"]
    actions = [a["action"] for a in world.client.get("/admin/audit", headers=h).json()["entries"]]
    assert "DATA_EXPORTED" in actions and "DATA_IMPORTED" in actions
