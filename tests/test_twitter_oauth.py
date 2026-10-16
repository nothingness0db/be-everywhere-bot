import base64
import time
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from apis.twitter import _upload_media
from apis.twitter_oauth import callback_code, exchange_token, refresh_if_needed
from apis.types import MediaItem
from db.accounts import create_account, get_credential, set_credentials
from utils.twitter_text import split_x_text


def test_callback_checks_state_and_redirect():
    assert (
        callback_code(
            "http://localhost/cb?code=abc&state=secret", "secret", "http://localhost/cb"
        )
        == "abc"
    )
    with pytest.raises(ValueError, match="state"):
        callback_code(
            "http://localhost/cb?code=abc&state=wrong", "secret", "http://localhost/cb"
        )
    with pytest.raises(ValueError, match="Callback URL"):
        callback_code(
            "https://other/cb?code=abc&state=secret", "secret", "http://localhost/cb"
        )
    with pytest.raises(ValueError, match="denied"):
        callback_code(
            "http://localhost/cb?error=denied&state=secret",
            "secret",
            "http://localhost/cb",
        )


@respx.mock
async def test_refresh_rotates_and_persists_tokens(engine):
    account = create_account(engine, "twitter", "main", "123")
    creds = {
        "client_id": "client",
        "bearer_token": "old",
        "refresh_token": "refresh-old",
        "token_expires_at": "1",
    }
    set_credentials(engine, account.id, creds)
    route = respx.post("https://api.x.com/2/oauth2/token").respond(
        200,
        json={
            "access_token": "new",
            "refresh_token": "refresh-new",
            "expires_in": 7200,
        },
    )
    result = await refresh_if_needed(engine, account.id, creds)
    assert result["bearer_token"] == "new"
    assert get_credential(engine, account.id, "refresh_token") == "refresh-new"
    assert (
        float(get_credential(engine, account.id, "token_expires_at"))
        > time.time() + 7000
    )
    body = parse_qs(route.calls[0].request.content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["refresh-old"]


@respx.mock
async def test_valid_token_needs_no_refresh(engine):
    creds = {"bearer_token": "valid", "token_expires_at": str(time.time() + 3600)}
    assert await refresh_if_needed(engine, 1, creds) == creds


@respx.mock
async def test_confidential_oauth_uses_basic_auth():
    route = respx.post("https://api.x.com/2/oauth2/token").respond(
        200, json={"access_token": "user", "refresh_token": "refresh"}
    )
    await exchange_token(
        {"client_id": "id", "client_secret": "secret"},
        {"grant_type": "authorization_code", "code": "code"},
    )
    assert (
        route.calls[0].request.headers["authorization"]
        == "Basic " + base64.b64encode(b"id:secret").decode()
    )


@respx.mock
async def test_failed_refresh_preserves_previous_credentials(engine):
    account = create_account(engine, "twitter", "main", "1")
    creds = {
        "client_id": "id",
        "bearer_token": "old",
        "refresh_token": "refresh",
        "token_expires_at": "1",
    }
    set_credentials(engine, account.id, creds)
    respx.post("https://api.x.com/2/oauth2/token").respond(
        400, json={"error": "invalid_grant"}
    )
    with pytest.raises(RuntimeError, match="OAuth failed"):
        await refresh_if_needed(engine, account.id, creds)
    assert get_credential(engine, account.id, "refresh_token") == "refresh"


@respx.mock
async def test_v2_media_upload_waits_for_processing(monkeypatch):
    async def no_sleep(seconds):
        pass

    monkeypatch.setattr("apis.twitter.asyncio.sleep", no_sleep)
    respx.post("https://api.x.com/2/media/upload/initialize").respond(
        200, json={"data": {"id": "123"}}
    )
    append = respx.post("https://api.x.com/2/media/upload/123/append").respond(204)
    respx.post("https://api.x.com/2/media/upload/123/finalize").respond(
        200,
        json={"data": {"processing_info": {"state": "pending", "check_after_secs": 1}}},
    )
    status = respx.get("https://api.x.com/2/media/upload").respond(
        200, json={"data": {"processing_info": {"state": "succeeded"}}}
    )
    async with httpx.AsyncClient() as client:
        assert (
            await _upload_media(client, "token", b"video", MediaItem("v", "video"))
            == "123"
        )
    assert append.call_count == 1 and status.call_count == 1


def test_chinese_and_ascii_weighted_split():
    assert split_x_text("中" * 200) == ["中" * 140, "中" * 60]
    assert split_x_text("a" * 300) == ["a" * 280, "a" * 20]


def test_links_remain_whole():
    link = "https://example.com/" + "a" * 300
    assert split_x_text("中" * 130 + " " + link) == ["中" * 130, link]
