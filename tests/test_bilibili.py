import json

import pytest
import respx

from apis.bilibili import publish_outbound
from apis.types import MediaItem, OutboundPost
from db.accounts import create_account, set_credentials


@pytest.fixture
def bilibili_account(engine):
    account = create_account(engine, "bilibili", "main", "123")
    set_credentials(
        engine,
        account.id,
        {"SESSDATA": "session", "bili_jct": "csrf", "buvid3": "device"},
    )
    return account


@respx.mock
async def test_publish_image_dynamic(engine, bilibili_account):
    upload = respx.post(
        "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs"
    ).respond(
        200,
        json={
            "code": 0,
            "data": {
                "image_url": "https://image.test/a.jpg",
                "image_width": 100,
                "image_height": 80,
            },
        },
    )
    create = respx.post("https://api.bilibili.com/x/dynamic/feed/create/dyn").respond(
        200,
        json={
            "code": 0,
            "data": {"dyn_id_str": "123456"},
        },
    )
    result = await publish_outbound(
        engine,
        bilibili_account.id,
        OutboundPost(
            text="图片说明",
            media=[MediaItem("tgfile:p1", "photo")],
        ),
        [b"jpeg"],
    )
    assert result.post_id == "123456"
    body = json.loads(create.calls[0].request.content)["dyn_req"]
    assert body["scene"] == 2
    assert body["pics"][0]["img_src"] == "https://image.test/a.jpg"
    assert create.calls[0].request.url.params["csrf"] == "csrf"
    assert "SESSDATA=session" in create.calls[0].request.headers["cookie"]
    assert b"jpeg" in upload.calls[0].request.content


@pytest.mark.parametrize("code", [-101, -111, -412])
@respx.mock
async def test_api_errors_do_not_record_success(engine, bilibili_account, code):
    respx.post("https://api.bilibili.com/x/dynamic/feed/create/dyn").respond(
        200, json={"code": code, "message": "failed"}
    )
    with pytest.raises(RuntimeError):
        await publish_outbound(engine, bilibili_account.id, OutboundPost("text"))


@respx.mock
async def test_video_is_rejected_before_request(engine, bilibili_account):
    with pytest.raises(ValueError, match="photos only"):
        await publish_outbound(
            engine,
            bilibili_account.id,
            OutboundPost("text", [MediaItem("v", "video")]),
            [b"video"],
        )


@respx.mock
async def test_missing_dynamic_id_is_not_success(engine, bilibili_account):
    respx.post("https://api.bilibili.com/x/dynamic/feed/create/dyn").respond(
        200, json={"code": 0, "data": {}}
    )
    with pytest.raises(RuntimeError, match="missing its ID"):
        await publish_outbound(engine, bilibili_account.id, OutboundPost("text"))
