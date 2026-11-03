import json

import pytest

from marketflow.collectors.base import parse_with_rules
from marketflow.rules import RuleStore, load_callable
from marketflow.units import UnitError, parse_unit
from datetime import date

DAY = date(2026, 11, 3)
DRIFTED = json.dumps({"data": {"error_code": "000", "item": [
    {"item_name": "사과", "kind_name": "후지", "rank": "상", "unit": "10kg", "price_today": "50,000"}]}})


def test_drifted_source_yields_nothing_until_rule_promoted(isolated_rules):
    # upstream renamed dpr1 -> price_today: old rules extract nothing (the self-healing trigger)
    assert parse_with_rules("KAMIS", DRIFTED, DAY) == []
    isolated_rules.promote(["sources", "KAMIS", "fields", "price"], "price_today", "md@example.com")
    recs = parse_with_rules("KAMIS", DRIFTED, DAY)   # no restart, no code change
    assert [str(r.avg_price) for r in recs] == ["50000"]


def test_promotion_lands_in_overlay_not_base(isolated_rules, tmp_path):
    isolated_rules.promote(["sources", "KAMIS", "fields", "price"], "price_today", "a")
    overlay = json.loads((tmp_path / "overrides.json").read_text())
    assert overlay == {"sources": {"KAMIS": {"fields": {"price": "price_today"}}}}
    assert json.loads((tmp_path / "extraction.json").read_text())["sources"]["KAMIS"]["fields"]["price"] == "dpr1"


def test_invalid_candidate_rejected_and_not_written(isolated_rules, tmp_path):
    with pytest.raises(Exception):
        isolated_rules.promote(["units", "pattern"], "([unclosed", "a")
    with pytest.raises(Exception):
        isolated_rules.promote(["sources", "KAMIS", "parser"], "marketflow.nope:missing", "a")
    assert not (tmp_path / "overrides.json").exists()


def test_regex_edit_on_disk_hot_reloads(isolated_rules, tmp_path):
    with pytest.raises(UnitError):
        parse_unit("10 박스")
    p = tmp_path / "extraction.json"
    cfg = json.loads(p.read_text())
    cfg["units"]["count_tokens"].append("박스")
    p.write_text(json.dumps(cfg, ensure_ascii=False))
    assert parse_unit("10 박스")[0] == "unit"


def test_broken_edit_keeps_last_good_rules(isolated_rules, tmp_path):
    isolated_rules.current()
    (tmp_path / "extraction.json").write_text("{ not json")
    assert parse_unit("10kg")[0] == "g"   # still served from last good set


def test_parser_swapped_via_config(isolated_rules, tmp_path):
    p = tmp_path / "extraction.json"
    cfg = json.loads(p.read_text())
    cfg["sources"]["KAMIS"]["parser"] = "marketflow.collectors.garak:parse_garak"
    p.write_text(json.dumps(cfg))
    assert load_callable(isolated_rules.current()["sources"]["KAMIS"]["parser"]).__name__ == "parse_garak"
