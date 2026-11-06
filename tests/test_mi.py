"""Market Intelligence: importer, category health, trend radar, search->demand lead check."""
from datetime import date, timedelta

import pytest

from marketflow.db import init_sqlite, make_engine
from marketflow.mi.analysis import category_health, leading_indicator, momentum, pearson, trend_radar
from marketflow.mi.config import MiConfig, load_mi_config
from marketflow.mi.importer import import_signals, parse_period
from marketflow.mi.store import MiStore
from marketflow.settings import Settings

W0 = date(2026, 9, 7)                                   # a Monday
WEEKS = [W0 + timedelta(weeks=i) for i in range(8)]
KEYWORDS = {                                            # (category, keyword) -> weekly searches
    ("Fruit", "grower"): [500, 520, 600, 700, 800, 900, 1000, 1100],
    ("Fruit", "bigfruit"): [1000] * 8,
    ("Fruit", "seedling"): [10, 10, 11, 12, 20, 24, 30, 40],
    ("Fruit", "emerger"): [5, 5, 6, 6, 10, 14, 18, 24],
    ("Fruit", "fader"): [900, 880, 800, 700, 500, 400, 300, 250],
    ("Fruit", "flat"): [40] * 8,
}


def obs(signal, period, category, keyword, metric, value):
    return {"signal_type": signal, "period_start": period, "category_l": category, "keyword": keyword, "metric": metric, "value": value}


def radar_rows(extra=()):
    rows = [obs("SEARCH", w, c, k, "searches", v) for (c, k), vals in KEYWORDS.items() for w, v in zip(WEEKS, vals)]
    return rows + list(extra)


def stage_of(radar, kw):
    return next(r["stage"] for r in radar if r["keyword"] == kw)


def test_momentum_uses_recent_vs_prior_window_and_degrades_gracefully():
    assert momentum([100, 100, 150, 150], 2) == pytest.approx(0.5)
    assert momentum([100, 120], 2) == pytest.approx(0.2)            # too short for two windows: last vs previous
    assert momentum([5], 2) is None
    assert momentum([0, 0, 5, 5], 2) == 5.0                          # from nothing: capped, not infinite
    assert momentum([0, 0, 0, 0], 2) == 0.0


def test_pearson_edges():
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert pearson([1, 1, 1], [1, 2, 3]) is None                    # no variance
    assert pearson([1, 2], [1, 2]) is None                          # too few points


def test_lifecycle_stages():
    cfg = MiConfig()
    rows = radar_rows([obs("PROMOTION", WEEKS[-1], "Fruit", "emerger", "count", 2)])
    radar = trend_radar(rows, [], cfg, include_stable=True)
    got = {r["keyword"]: r["stage"] for r in radar}
    assert got == {"grower": "GROWING", "bigfruit": "MAINSTREAM", "seedling": "SEED", "emerger": "EMERGING", "fader": "FADING", "flat": "STABLE"}
    assert "flat" not in {r["keyword"] for r in trend_radar(rows, [], cfg)}      # flat, low-volume keywords are noise by default
    assert "top 1% in Fruit" in next(r for r in radar if r["keyword"] == "grower")["why"]     # the highest-volume keyword is "top 1%", not "top 0%"
    emerger = next(r for r in radar if r["keyword"] == "emerger")
    assert emerger["corroborating"] == ["promotion"] and emerger["signals"]["promotion"] == 2


def test_radar_ranks_early_rising_keywords_above_falling_and_established_ones():
    rows = radar_rows([obs("MEDIA", WEEKS[-1], "Fruit", "emerger", "count", 3)])
    order = [r["keyword"] for r in trend_radar(rows, [], MiConfig())]
    assert order.index("emerger") < order.index("seedling") < order.index("grower")     # corroborated + low volume = biggest opportunity
    assert order.index("grower") < order.index("bigfruit") < order.index("fader")
    fader = next(r for r in trend_radar(rows, [], MiConfig()) if r["keyword"] == "fader")
    assert fader["score"] == 0 and fader["opportunity"] == 0


def test_weights_change_the_score():
    rows = radar_rows([obs("MEDIA", WEEKS[-1], "Fruit", "seedling", "count", 3)])
    base = next(r for r in trend_radar(rows, [], MiConfig()) if r["keyword"] == "seedling")["score"]
    heavy = next(r for r in trend_radar(rows, [], load_mi_config({"weights": {"media": 5}})) if r["keyword"] == "seedling")["score"]
    assert heavy > base
    with pytest.raises(ValueError):
        load_mi_config({"weights": {"nonsense": 1}})
    with pytest.raises(ValueError):
        load_mi_config({"weights": {"search": 0, "product": 0, "promotion": 0, "media": 0}})


def test_keywords_with_too_little_history_get_no_stage():
    rows = [obs("SEARCH", w, "Fruit", "newcomer", "searches", v) for w, v in zip(WEEKS[:3], [1, 5, 25])]
    assert trend_radar(rows, [], MiConfig(), include_stable=True) == []


def test_category_health_demand_share_supply_and_story():
    rows = []
    for i, w in enumerate(WEEKS):
        rows += [obs("BUYER", w, "Fruit", "", "gmv", 1000 + 100 * i), obs("BUYER", w, "Veg", "", "gmv", 2000 - 100 * i),
                 obs("SEARCH", w, "Fruit", "", "searches", 100 + 10 * i)]
    health = {h["category"]: h for h in category_health(rows, {"Fruit": {"stock": 20, "daily": 10}, "Veg": {"stock": 900, "daily": 10}}, MiConfig())}
    fruit, veg = health["Fruit"], health["Veg"]
    assert (fruit["demand_state"], veg["demand_state"]) == ("GROWING", "WEAKENING")
    assert fruit["share_shift_pp"] > 0 > veg["share_shift_pp"]                       # demand moved from Veg to Fruit
    assert fruit["share_pct"] + veg["share_pct"] == pytest.approx(100, abs=0.2)
    assert fruit["supply"]["status"] == "TIGHT" and veg["supply"]["status"] == "SURPLUS"
    assert "secure supply" in fruit["action"] and "clearing stock" in veg["action"]
    assert fruit["demand_series"][-1] == [WEEKS[-1].isoformat(), "1700"]
    assert [h["category"] for h in category_health(rows, {}, MiConfig())][0] == "Veg"     # weakening first


def test_category_total_prefers_explicit_row_else_sums_keywords():
    rows = [obs("SEARCH", WEEKS[0], "Fruit", "a", "searches", 10), obs("SEARCH", WEEKS[0], "Fruit", "b", "searches", 5),
            obs("SEARCH", WEEKS[1], "Fruit", "a", "searches", 10), obs("SEARCH", WEEKS[1], "Fruit", "", "searches", 99)]
    h = category_health(rows + [obs("BUYER", WEEKS[0], "Fruit", "", "gmv", 1)], {}, MiConfig())[0]
    assert h["interest_series"] == [[WEEKS[0].isoformat(), "15"], [WEEKS[1].isoformat(), "99"]]


def lagged_series(lag):
    changes = [0.1, -0.2, 0.3, -0.1, 0.25, -0.3, 0.15, 0.2, -0.25, 0.1, -0.05, 0.2]
    s, d = [100.0], [100.0]
    for c in changes:
        s.append(s[-1] * (1 + c))
    dch = [0.0] * lag + changes[:len(changes) - lag]
    for c in dch:
        d.append(d[-1] * (1 + c))
    days = [W0 + timedelta(weeks=i) for i in range(len(s))]
    return list(zip(days, s)), list(zip(days, d))


def test_search_leading_demand_is_detected_only_when_the_data_shows_it():
    s, d = lagged_series(1)
    r = leading_indicator(s, d, MiConfig())
    assert (r["verdict"], r["lag"]) == ("LEADS", 1) and r["r"] > 0.9
    s0, d0 = lagged_series(0)
    assert leading_indicator(s0, d0, MiConfig())["verdict"] == "COINCIDENT"
    constant = [(day, 100.0) for day, _ in s]
    assert leading_indicator(constant, d, MiConfig())["verdict"] == "INSUFFICIENT"      # no variation in searches: nothing to test
    unrelated = [(day, v) for (day, _), v in zip(s, [85, 114, 111, 90, 100, 98, 106, 112, 84, 81, 113, 97, 110])]
    assert leading_indicator(unrelated, d, MiConfig())["verdict"] == "NOT_VERIFIED"
    assert leading_indicator(s[:4], d[:4], MiConfig())["verdict"] == "INSUFFICIENT"


def test_unverified_search_is_labelled_interest_not_demand():
    rows = radar_rows([obs("BUYER", w, "Fruit", "", "gmv", 1000) for w in WEEKS])
    health = category_health(rows, {}, MiConfig())
    seed = next(r for r in trend_radar(rows, health, MiConfig()) if r["keyword"] == "seedling")
    assert "interest, not demand" in seed["why"]


# ---- import --------------------------------------------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'mi.db'}")
    init_sqlite(e)
    return MiStore(e)


def test_parse_period_formats():
    assert parse_period("2026-10-26") == date(2026, 10, 26)
    assert parse_period("20261026") == date(2026, 10, 26)
    assert parse_period("202510") == date(2025, 10, 1)
    assert parse_period("202510.0") == date(2025, 10, 1)
    assert parse_period("2026-W43") == date(2026, 10, 19)           # the Monday of ISO week 43
    with pytest.raises(ValueError):
        parse_period("last week")


def test_import_search_report_cp949_with_bad_cells(store, tmp_path):
    f = tmp_path / "search.csv"
    f.write_bytes("week,keyword,category,searches,buyers,gmv\n2026-W43,계란,Fruit,\"440,059\",147710,1693234450\n"
                  "2026-W43,바나나,Fruit,312147,abc,450073630\nnot-a-week,사과,Fruit,1,1,1\n".encode("cp949"))
    res = import_signals(f, store, "search", "hive")
    assert res.loaded == 5 and [(e.line, e.field) for e in res.errors] == [(3, "buyers"), (4, "period")]
    rows = {(r["keyword"], r["metric"]): r["value"] for r in store.observations(10)}
    assert rows[("계란", "searches")] == 440059 and ("바나나", "buyers") not in rows


def test_reimport_overwrites_the_same_period_and_never_duplicates(store, tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("week,category,searches\n2026-10-19,Fruit,100\n2026-10-19,Fruit,150\n", encoding="utf-8")
    assert import_signals(f, store, "SEARCH").loaded == 1             # a repeated key inside one file: last row wins
    f.write_text("week,category,searches\n2026-10-19,Fruit,200\n", encoding="utf-8")
    import_signals(f, store, "SEARCH")
    got = store.observations(10)
    assert len(got) == 1 and got[0]["value"] == 200


def test_import_rejects_unknown_type_and_missing_columns(store, tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("category,searches\nFruit,1\n", encoding="utf-8")
    assert "no period column" in import_signals(f, store, "SEARCH").errors[0].message
    f.write_text("week,category\n2026-10-19,Fruit\n", encoding="utf-8")
    assert "no metric columns" in import_signals(f, store, "SEARCH").errors[0].message
    with pytest.raises(ValueError):
        import_signals(f, store, "WEATHER")


def test_history_window_keeps_only_the_newest_periods(store, tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("week,searches\n" + "\n".join(f"{(W0 + timedelta(weeks=i)).isoformat()},{i + 1}" for i in range(10)) + "\n", encoding="utf-8")
    import_signals(f, store, "SEARCH")
    assert sorted({r["period_start"] for r in store.observations(3)}) == [W0 + timedelta(weeks=i) for i in (7, 8, 9)]


def test_cli_import_and_report_end_to_end(tmp_path):
    import io
    from marketflow.cli import main
    s = Settings(database_url=f"sqlite:///{tmp_path / 'cli.db'}")
    f = tmp_path / "search.csv"
    f.write_text("week,category,keyword,searches\n" + "\n".join(
        f"{w.isoformat()},{c},{k},{v}" for (c, k), vals in KEYWORDS.items() for w, v in zip(WEEKS, vals)) + "\n", encoding="utf-8")
    out = io.StringIO()
    assert main(["import-mi", str(f), "--type", "SEARCH"], settings=s, out=out) == 0 and "loaded=48 errors=0" in out.getvalue()
    out = io.StringIO()
    assert main(["mi-report"], settings=s, out=out) == 0
    text = out.getvalue()
    assert "TREND RADAR" in text and "seedling" in text and "SEED" in text and "\n  flat " not in text
    assert main(["import-mi", str(f), "--type", "WEATHER"], settings=s, out=io.StringIO()) == 2


def test_volume_rank_needs_three_peers_so_a_lone_keyword_is_never_called_growing_or_mainstream():
    rows = [obs("SEARCH", w, "Fruit", k, "searches", v) for k, vals in {"a": [100, 100, 100, 200, 200, 300], "b": [50] * 6}.items() for w, v in zip(WEEKS, vals)]
    got = {r["keyword"]: (r["stage"], r["volume_percentile"]) for r in trend_radar(rows, [], MiConfig(), include_stable=True)}
    assert got == {"a": ("SEED", None), "b": ("STABLE", None)}


def test_every_message_exists_in_both_languages_with_the_same_placeholders():
    import string
    from marketflow.mi.messages import MESSAGES
    assert set(MESSAGES["en"]) == set(MESSAGES["ko"])
    fields = lambda t: {f for _, f, _, _ in string.Formatter().parse(t) if f}       # noqa: E731
    for k in MESSAGES["en"]:
        assert fields(MESSAGES["en"][k]) == fields(MESSAGES["ko"][k]), k


def test_narratives_come_out_in_korean_on_request():
    rows = radar_rows([obs("PROMOTION", WEEKS[-1], "Fruit", "emerger", "count", 2)] + [obs("BUYER", w, "Fruit", "", "gmv", 1000 + 100 * i) for i, w in enumerate(WEEKS)])
    health = category_health(rows, {"Fruit": {"stock": 10, "daily": 10}}, MiConfig(), "ko")
    assert "GMV이(가)" in health[0]["changed"] and "증가했습니다" in health[0]["changed"] and "재고는 판매 1.0일치뿐입니다" in health[0]["why"]
    emerger = next(r for r in trend_radar(rows, health, MiConfig(), lang="ko") if r["keyword"] == "emerger")
    assert "프로모션" in emerger["why"] and emerger["action"].startswith("상품 구성 검토") and "검색이" in emerger["changed"]
    en = next(r for r in trend_radar(rows, health, MiConfig()) if r["keyword"] == "emerger")
    assert "promotion" in en["why"] and en["action"].startswith("Review assortment")
