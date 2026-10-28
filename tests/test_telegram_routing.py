import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import apis.telegram as telegram
from db.accounts import create_account, get_credential, set_credentials
from db.connection import get_engine
from db.sync_state import is_synced
from db.telegram_inbox import read_messages, save_page
from sync.engine import run_sync
from sync.routing import RoutingConfig

DUAL_ROUTING = RoutingConfig(prefixes={"x": ("twitter",), "b": ("bilibili",)})


@pytest.fixture
def accounts(engine):
    tg = create_account(engine, "telegram", "main", "@news")
    x = create_account(engine, "twitter", "main", "x-user")
    b = create_account(engine, "bilibili", "main", "b-user")
    set_credentials(
        engine,
        tg.id,
        {"bot_token": "token", "channel_id": "@news", "update_offset": "0"},
    )
    set_credentials(
        engine,
        x.id,
        {"bearer_token": "user-token", "user_id": "x-user", "username": "user"},
    )
    set_credentials(engine, b.id, {"SESSDATA": "session", "bili_jct": "csrf"})
    return tg, x, b


def message(mid, text, **extra):
    return {
        "message_id": mid,
        "date": int(datetime.now(timezone.utc).timestamp()),
        "text": text,
        "chat": {"id": -1001, "username": "news"},
        **extra,
    }


def mock_updates(messages):
    return respx.get("https://api.telegram.org/bottoken/getUpdates").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": i + 1, "channel_post": msg}
                        for i, msg in enumerate(messages)
                    ],
                },
            ),
            httpx.Response(200, json={"ok": True, "result": []}),
        ]
    )


@respx.mock
async def test_prefix_routing_and_deduplication(engine, accounts):
    mock_updates(
        [
            message(1, "只留在TG"),
            message(2, "发X #x"),
            message(3, "发B站 #b"),
            message(4, "都发 #x #b"),
        ]
    )
    x = respx.post("https://api.x.com/2/tweets").mock(
        side_effect=[
            httpx.Response(201, json={"data": {"id": "x1"}}),
            httpx.Response(201, json={"data": {"id": "x2"}}),
        ]
    )
    b = respx.post("https://api.bilibili.com/x/dynamic/feed/create/dyn").mock(
        side_effect=[
            httpx.Response(200, json={"code": 0, "data": {"dyn_id_str": "b1"}}),
            httpx.Response(200, json={"code": 0, "data": {"dyn_id_str": "b2"}}),
        ]
    )
    assert await run_sync(engine, routing=DUAL_ROUTING) == 4
    assert [json.loads(c.request.content)["text"] for c in x.calls] == ["发X", "都发"]
    assert [
        json.loads(c.request.content)["dyn_req"]["content"]["contents"][0]["raw_text"]
        for c in b.calls
    ] == ["发B站\n\n转自我的tg频道", "都发\n\n转自我的tg频道"]
    respx.get("https://api.telegram.org/bottoken/getUpdates").respond(
        200, json={"ok": True, "result": []}
    )
    assert await run_sync(engine, routing=DUAL_ROUTING) == 0
    assert x.call_count == 2 and b.call_count == 2
    assert read_messages(engine, accounts[0].id)[1]["text"] == "发X #x"


@respx.mock
async def test_failed_destination_retries_after_restart_without_reposting_success(
    engine, accounts
):
    tg, x_account, b_account = accounts
    msg = message(42, "retry #x #b")
    msg["date"] -= 10 * 86400  # Pending inbox entries must outlive the watch overlap.
    mock_updates([msg])
    x = respx.post("https://api.x.com/2/tweets").respond(
        201, json={"data": {"id": "x1"}}
    )
    b = respx.post("https://api.bilibili.com/x/dynamic/feed/create/dyn").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"code": 0, "data": {"dyn_id_str": "b1"}}),
        ]
    )
    assert await run_sync(engine, routing=DUAL_ROUTING) == 1
    assert is_synced(engine, tg.id, "42", x_account.id)
    assert not is_synced(engine, tg.id, "42", b_account.id)
    restarted = get_engine(Path(engine.url.database))
    respx.get("https://api.telegram.org/bottoken/getUpdates").respond(
        200, json={"ok": True, "result": []}
    )
    assert await run_sync(restarted, routing=DUAL_ROUTING) == 1
    assert x.call_count == 1 and b.call_count == 2


@respx.mock
async def test_partial_x_thread_resumes_at_failed_part(engine, accounts):
    mock_updates([message(1, "中" * 200 + " #x")])
    x = respx.post("https://api.x.com/2/tweets").mock(
        side_effect=[
            httpx.Response(201, json={"data": {"id": "first"}}),
            httpx.Response(503),
            httpx.Response(201, json={"data": {"id": "second"}}),
        ]
    )
    assert await run_sync(engine, routing=DUAL_ROUTING) == 0
    respx.get("https://api.telegram.org/bottoken/getUpdates").respond(
        200, json={"ok": True, "result": []}
    )
    assert await run_sync(engine, routing=DUAL_ROUTING) == 1
    assert x.call_count == 3
    bodies = [json.loads(c.request.content) for c in x.calls]
    assert bodies[0]["text"] == "中" * 140
    assert bodies[2] == {"text": "中" * 60, "reply": {"in_reply_to_tweet_id": "first"}}


@respx.mock
async def test_one_bot_collects_all_configured_channels(engine, accounts):
    other = create_account(engine, "telegram", "second", "@other")
    set_credentials(engine, other.id, {"bot_token": "token", "channel_id": "@other"})
    first = message(1, "one #x")
    second = message(2, "two #b", chat={"id": -1002, "username": "other"})
    mock_updates([first, second])
    posts = await telegram.fetch_posts(engine, accounts[0].id)
    assert [p.text for p in posts] == ["one #x"]
    assert [m["text"] for m in read_messages(engine, other.id)] == ["two #b"]
    assert get_credential(engine, other.id, "update_offset") == "3"


def test_album_settles_after_last_slide(engine, accounts, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("db.telegram_inbox.time.time", lambda: now[0])
    tg = accounts[0]
    slide = message(
        10,
        "",
        caption="album #x #b",
        media_group_id="album",
        photo=[{"file_id": "photo1"}],
    )
    save_page(engine, [(tg.id, slide)], [tg.id], 1)
    now[0] = 101
    second = message(11, "", media_group_id="album", photo=[{"file_id": "photo2"}])
    save_page(engine, [(tg.id, second)], [tg.id], 2)
    assert read_messages(engine, tg.id, settle_seconds=2) == []
    now[0] = 104
    posts = telegram._merge_album_posts(
        [telegram._message_to_post(m, "@news") for m in read_messages(engine, tg.id)]
    )
    assert len(posts) == 1 and len(posts[0].media) == 2
    assert posts[0].text == "album #x #b"


def test_inbox_and_offset_roll_back_together(engine, accounts):
    tg = accounts[0]
    with pytest.raises(KeyError):
        save_page(engine, [(tg.id, message(1, "text #x")), (tg.id, {})], [tg.id], 10)
    assert read_messages(engine, tg.id) == []
    assert get_credential(engine, tg.id, "update_offset") == "0"


@respx.mock
async def test_telegram_error_does_not_expose_bot_token(engine, accounts):
    respx.get("https://api.telegram.org/bottoken/getUpdates").respond(
        401, json={"ok": False, "description": "Unauthorized"}
    )
    with pytest.raises(RuntimeError) as error:
        await telegram.fetch_posts(engine, accounts[0].id)
    assert "bottoken" not in str(error.value)
    assert "Unauthorized" in str(error.value)


@respx.mock
async def test_mesh_mode_remains_explicitly_available(
    engine, accounts, monkeypatch, post_factory
):
    monkeypatch.setattr(
        "apis.twitter.fetch_posts",
        AsyncMock(return_value=[post_factory("1", text="no prefix")]),
    )
    respx.get("https://api.telegram.org/bottoken/getUpdates").respond(
        200, json={"ok": True, "result": []}
    )
    tg = respx.post("https://api.telegram.org/bottoken/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 55}}
    )
    b = respx.post("https://api.bilibili.com/x/dynamic/feed/create/dyn").respond(
        200, json={"code": 0, "data": {"dyn_id_str": "b1"}}
    )
    assert await run_sync(engine, routing=RoutingConfig(mode="mesh")) == 2
    assert tg.call_count == 1 and b.call_count == 1


@respx.mock
async def test_album_caption_routes_both_photos_to_both_platforms(engine, accounts):
    first = message(
        10, "", caption="album #x #b", media_group_id="group", photo=[{"file_id": "one"}]
    )
    second = message(11, "", media_group_id="group", photo=[{"file_id": "two"}])
    mock_updates([first, second])
    for file_id in ("one", "two"):
        respx.get(
            "https://api.telegram.org/bottoken/getFile", params={"file_id": file_id}
        ).respond(200, json={"ok": True, "result": {"file_path": file_id + ".jpg"}})
        respx.get(f"https://api.telegram.org/file/bottoken/{file_id}.jpg").respond(
            200, content=file_id.encode()
        )
    respx.post("https://api.x.com/2/media/upload/initialize").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"id": "101"}}),
            httpx.Response(200, json={"data": {"id": "102"}}),
        ]
    )
    append_one = respx.post("https://api.x.com/2/media/upload/101/append").respond(204)
    append_two = respx.post("https://api.x.com/2/media/upload/102/append").respond(204)
    for media_id in ("101", "102"):
        respx.post(f"https://api.x.com/2/media/upload/{media_id}/finalize").respond(
            200, json={"data": {"id": media_id}}
        )
    x = respx.post("https://api.x.com/2/tweets").respond(
        201, json={"data": {"id": "x1"}}
    )
    upload_b = respx.post(
        "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs"
    ).respond(
        200,
        json={
            "code": 0,
            "data": {
                "image_url": "https://img.test/p.jpg",
                "image_width": 10,
                "image_height": 20,
            },
        },
    )
    b = respx.post("https://api.bilibili.com/x/dynamic/feed/create/dyn").respond(
        200, json={"code": 0, "data": {"dyn_id_str": "b1"}}
    )
    assert (
        await run_sync(
            engine,
            routing=RoutingConfig(
                album_settle_seconds=0, prefixes=DUAL_ROUTING.prefixes
            ),
        )
        == 2
    )
    assert json.loads(x.calls[0].request.content) == {
        "text": "album",
        "media": {"media_ids": ["101", "102"]},
    }
    dynamic = json.loads(b.calls[0].request.content)["dyn_req"]
    assert len(dynamic["pics"]) == 2 and upload_b.call_count == 2
    assert (
        base64.b64decode(json.loads(append_one.calls[0].request.content)["media"])
        == b"one"
    )
    assert (
        base64.b64decode(json.loads(append_two.calls[0].request.content)["media"])
        == b"two"
    )
