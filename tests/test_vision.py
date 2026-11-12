"""Flyer reading through the OpenAI API: request shape, validation of what comes back, and the flyer crawler around it."""
import json
import struct
import zlib

import pytest

from marketflow.vision import OpenAIVision, VisionError, image_mime, parse_items
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
async def test_flyer_crawler_reads_images_into_weekly_promotion_mentions(world):    # noqa: F811
    d = world.inbox / "flyer"; d.mkdir(parents=True)
    (d / "a.png").write_bytes(png()); (d / "b.png").write_bytes(png()); (d / "bad.png").write_bytes(b"not an image")
    async def post(url, body, headers):
        return reply([{"name": "사과", "price": 1000}, {"name": "딸기", "price": None}])
    world.svc.registry["flyer"]._vision = OpenAIVision("k", post=post)
    await world.svc.run("flyer", "MANUAL", "kim")
    obs = [o for o in world.mi.observations(8) if o["signal_type"] == "PROMOTION"]
    assert {(o["keyword"], float(o["value"])) for o in obs} == {("사과", 2.0), ("딸기", 2.0)}      # two readable images, one mention each
    assert all(str(o["period_start"])[:10] == "2026-11-02" for o in obs)                   # the Monday of the run week
    assert sorted(p.name[16:] for p in (d / "processed").iterdir()) == ["a.png", "b.png"] and len(list((d / "failed").iterdir())) == 1
    assert not list(d.glob("*.png"))
    assert "failed" in json.dumps(world.svc.detail("flyer")["runs"][0])
