"""Flyer reading through the OpenAI API: request shape, validation of what comes back, and the flyer crawler around it."""
import json
import struct
import zlib

import pytest

from marketflow import flyers
from datetime import date
from marketflow.vision import FlyerItem, OpenAIVision, VisionError, image_mime, parse_items
from test_crawlers import world  # noqa: F401


def png() -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b"")


def reply(items):
    return 200, {"choices": [{"message": {"content": json.dumps({"items": items})}}]}


def test_mime_comes_from_the_bytes():
    assert image_mime(png()) == "image/png" and image_mime(b"\xff\xd8\xff\xe0x") == "image/jpeg" and image_mime(b"RIFF\0\0\0\0WEBPx") == "image/webp"
    assert image_mime(b"<svg onload=x>") is None and image_mime(b"%PDF-1.4") is None


def test_parse_items_validates_names_prices_and_personal_data():
    got = parse_items({"items": [{"name": " 사과  3kg ", "price": 9900}, {"name": "딸기", "price": "싸다"}, {"name": "문의 010-1234-5678", "price": 100},
                                 {"name": "x", "price": 5}, {"name": "배추", "price": True}, {"name": "양파", "price": -3}, "junk", {"price": 1}]})
    assert [(i.name, i.price) for i in got] == [("사과 3kg", 9900), ("딸기", None), ("배추", None), ("양파", None)]
    with pytest.raises(VisionError):
        parse_items({"nope": 1})


@pytest.mark.asyncio
async def test_request_carries_the_image_and_the_key_only_in_the_header():
    seen = {}
    async def post(url, body, headers):
        seen.update(url=url, body=body, headers=headers); return reply([{"name": "사과", "price": 1000}])
    v = OpenAIVision("sk-test", "gpt-4o-mini", post)
    items = await v.read_flyer(png())
    assert [(i.name, i.price) for i in items] == [("사과", 1000)]
    assert seen["url"].startswith("https://api.openai.com/") and seen["headers"] == {"Authorization": "Bearer sk-test"}
    assert "sk-test" not in json.dumps(seen["body"]) and seen["body"]["model"] == "gpt-4o-mini"
    assert seen["body"]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_errors_do_not_leak_bodies_and_bad_input_is_refused_before_sending():
    async def boom(url, body, headers):
        return 401, {"error": {"message": "Incorrect API key sk-live-secret"}}
    with pytest.raises(VisionError) as e:
        await OpenAIVision("k", post=boom).read_flyer(png())
    assert str(e.value) == "OpenAI HTTP 401" and "secret" not in str(e.value)
    async def never(*a):
        raise AssertionError("must not be called")
    with pytest.raises(VisionError):
        await OpenAIVision("k", post=never).read_flyer(b"%PDF-1.4 not an image")
    with pytest.raises(VisionError):
        await OpenAIVision("k", post=never).read_flyer(png() + b"0" * (5 * 1024 * 1024))
    with pytest.raises(VisionError):
        OpenAIVision("")


@pytest.mark.asyncio
async def test_flyer_crawler_without_a_key_leaves_images_alone(world):          # noqa: F811
    d = world.inbox / "flyer"; d.mkdir(parents=True); (d / "a.png").write_bytes(png())
    out = await world.svc.run("flyer", "MANUAL", "kim")
    assert (d / "a.png").exists()
    assert "OpenAI key" in json.dumps(world.svc.detail("flyer")["runs"][0]["message"])


@pytest.mark.asyncio
async def test_flyer_crawler_holds_what_it_reads_for_review_and_nothing_reaches_analysis(world):    # noqa: F811
    d = world.inbox / "flyer"; d.mkdir(parents=True)
    (d / "a.png").write_bytes(png()); (d / "b.png").write_bytes(png()); (d / "bad.png").write_bytes(b"not an image")
    async def post(url, body, headers):
        return reply([{"name": "사과", "price": 1000, "confidence": 0.95}, {"name": "딸기", "price": None, "confidence": 0.4}])
    world.svc.registry["flyer"]._vision = OpenAIVision("k", post=post)
    await world.svc.run("flyer", "MANUAL", "kim")
    assert not [o for o in world.mi.observations(8) if o["signal_type"] == "PROMOTION"]           # unreviewed data never reaches analysis
    fl = flyers.list_flyers(world.mi)
    assert len(fl) == 2 and all(f["pending"] == 2 for f in fl)
    one = flyers.get_flyer(world.mi, fl[0]["flyer_id"])
    assert [(i["name"], i["low_confidence"]) for i in one["items"]] == [("사과", False), ("딸기", True)]
    assert (world.inbox / flyers.image_path(world.mi, fl[0]["flyer_id"])).is_file() and len(list((d / "failed").iterdir())) == 1 and not list(d.glob("*.png"))
    assert "failed" in json.dumps(world.svc.detail("flyer")["runs"][0])


@pytest.mark.asyncio
async def test_review_approves_fixes_and_recomputes_the_week(world):                          # noqa: F811
    d = world.inbox / "flyer"; d.mkdir(parents=True)
    (d / "a.png").write_bytes(png()); (d / "b.png").write_bytes(png())
    async def post(url, body, headers):
        return reply([{"name": "사과", "price": 1000, "confidence": 0.95}, {"name": "딸기", "price": None, "confidence": 0.4}])
    world.svc.registry["flyer"]._vision = OpenAIVision("k", post=post)
    await world.svc.run("flyer", "MANUAL", "kim")
    a, b = [f["flyer_id"] for f in flyers.list_flyers(world.mi)]
    now = world.tp.now().replace(tzinfo=None)
    ia = flyers.get_flyer(world.mi, a)["items"]; ib = flyers.get_flyer(world.mi, b)["items"]
    flyers.review_flyer(world.mi, "kim", now, a, [{"item_id": ia[0]["item_id"], "action": "approve"}, {"item_id": ia[1]["item_id"], "action": "approve", "name": "딸기 특품"}])
    flyers.review_flyer(world.mi, "kim", now, b, [{"item_id": ib[0]["item_id"], "action": "approve"}, {"item_id": ib[1]["item_id"], "action": "reject"}])
    obs = {o["keyword"]: float(o["value"]) for o in world.mi.observations(8) if o["signal_type"] == "PROMOTION"}
    assert obs == {"사과": 2.0, "딸기 특품": 1.0}                                                  # rejected strawberry never counted
    assert flyers.get_flyer(world.mi, a)["status"] == "REVIEWED"
    flyers.review_flyer(world.mi, "kim", now, b, [{"item_id": ib[0]["item_id"], "action": "reject"}])      # a later change is reflected
    assert {o["keyword"]: float(o["value"]) for o in world.mi.observations(8) if o["signal_type"] == "PROMOTION"} == {"사과": 1.0, "딸기 특품": 1.0}
    assert "FLYER_REVIEWED" in {x["action"] for x in world.mi.audit_entries()}
    for bad in ({"item_id": 9999, "action": "approve"}, {"item_id": ia[0]["item_id"], "action": "maybe"}, {"item_id": ia[0]["item_id"], "action": "approve", "name": "연락 010-1234-5678"},
                {"item_id": ia[0]["item_id"], "action": "approve", "price": -5}):
        with pytest.raises(ValueError):
            flyers.review_flyer(world.mi, "kim", now, a, [bad])
    with pytest.raises(KeyError):
        flyers.review_flyer(world.mi, "kim", now, 999, [{"item_id": 1, "action": "approve"}])


def test_parse_items_keeps_a_clamped_confidence():
    got = parse_items({"items": [{"name": "사과", "price": 1, "confidence": 1.7}, {"name": "배추", "confidence": "high"}, {"name": "양파", "confidence": -2}]})
    assert [i.confidence for i in got] == [1.0, None, 0.0]


def test_flyer_api_review_and_image_are_for_deciders_and_stay_inside_the_inbox(world, monkeypatch):   # noqa: F811
    import asyncio
    from fastapi.testclient import TestClient
    from marketflow.web.app import create_app
    from marketflow.web.repo import AppRepository
    from marketflow.web.security import hash_password
    monkeypatch.setattr("marketflow.web.security._N", 2 ** 4)
    d = world.inbox / "flyer"; (d / "review").mkdir(parents=True); (d / "review" / "x.png").write_bytes(png())
    fid = flyers.add_flyer(world.mi, "x.png", "flyer/review/x.png", date(2026, 11, 2), [FlyerItem("사과", 1000, 0.9)], world.tp.now().replace(tzinfo=None))
    evil = flyers.add_flyer(world.mi, "evil", "../c.db", date(2026, 11, 2), [], world.tp.now().replace(tzinfo=None))
    repo = AppRepository(world.e)
    for email, role in (("kim@x.com", "MD"), ("b@x.com", "BUYER")):
        repo.set_assignments(repo.create_user(email, email[:3], role, hash_password("correct-horse-battery")), [("Fresh", "Fruit")])
    s = world.s.__class__(**{**world.s.__dict__, "jwt_secret": "j" * 48})
    c = TestClient(create_app(s, world.e, world.tp))
    h = lambda e: {"Authorization": "Bearer " + c.post("/auth/login", json={"email": e, "password": "correct-horse-battery"}).json()["access_token"]}   # noqa: E731
    assert c.get("/flyers", headers=h("b@x.com")).status_code == 403
    assert c.get(f"/flyers/{fid}/image", headers=h("b@x.com")).status_code == 403
    img = c.get(f"/flyers/{fid}/image", headers=h("kim@x.com"))
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert c.get(f"/flyers/{evil}/image", headers=h("kim@x.com")).status_code == 404            # a path outside the inbox is never served
    item = c.get(f"/flyers/{fid}", headers=h("kim@x.com")).json()["items"][0]["item_id"]
    assert c.post(f"/flyers/{fid}/review", json={"decisions": [{"item_id": 999, "action": "approve"}]}, headers=h("kim@x.com")).status_code == 422
    ok = c.post(f"/flyers/{fid}/review", json={"decisions": [{"item_id": item, "action": "approve", "price": 1200}]}, headers=h("kim@x.com"))
    assert ok.status_code == 200 and ok.json()["status"] == "REVIEWED" and ok.json()["items"][0]["price"] == 1200
    assert c.get("/flyers/999", headers=h("kim@x.com")).status_code == 404
