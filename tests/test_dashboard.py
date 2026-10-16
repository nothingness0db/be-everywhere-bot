import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from dashboard.app import create_app
from db.accounts import create_account, get_all_credentials, set_credentials
from db.health import get_health
from db.jobs import ensure_job, finish_job, get_job, start_job
from sync.engine import publish_tracked
from sync.routing import RoutingConfig
from sync.runtime import SyncRuntime

PASSWORD = "local-dashboard-test-password"


@pytest.fixture
def runtime(engine):
    return SyncRuntime(engine, RoutingConfig())


@pytest.fixture
def accounts(engine):
    tg = create_account(engine, "telegram", "main", "@news")
    x = create_account(engine, "twitter", "main", "123")
    b = create_account(engine, "bilibili", "main", "456")
    set_credentials(
        engine, tg.id, {"bot_token": "tg-secret-token", "channel_id": "@news"}
    )
    set_credentials(
        engine,
        x.id,
        {"bearer_token": "x-secret-token", "username": "writer", "user_id": "123"},
    )
    set_credentials(
        engine,
        b.id,
        {
            "SESSDATA": "bili-secret-cookie",
            "bili_jct": "csrf-secret",
            "username": "writer",
        },
    )
    return tg, x, b


@pytest.fixture
async def client(runtime):
    app = create_app(runtime, PASSWORD)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test"
    ) as client:
        yield client


async def login(client):
    response = await client.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 200
    csrf = (await client.get("/api/session")).json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    return csrf


def test_dashboard_requires_a_password(runtime):
    with pytest.raises(ValueError, match="DASHBOARD_PASSWORD"):
        create_app(runtime, "")


async def test_authentication_and_logout(client):
    assert (await client.get("/")).status_code == 303
    assert (await client.get("/api/overview")).status_code == 401
    assert (await client.get("/login")).status_code == 200
    assert (await client.get("/assets/app.js")).status_code == 200
    assert (
        await client.post("/api/login", json={"password": "wrong"})
    ).status_code == 401
    await login(client)
    cookie = client.cookies.get("sync_dashboard")
    response = await client.get("/")
    assert response.status_code == 200
    assert "同步工作台" in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert (await client.post("/api/logout")).status_code == 200
    client.cookies.set("sync_dashboard", cookie)
    assert (await client.get("/api/overview")).status_code == 401


async def test_mutations_require_csrf_and_same_origin(client):
    await login(client)
    assert (
        await client.post("/api/logout", headers={"X-CSRF-Token": "wrong"})
    ).status_code == 403
    assert (
        await client.post("/api/logout", headers={"Origin": "https://foreign.test"})
    ).status_code == 403
    assert (
        await client.post("/api/logout", headers={"Origin": "http://dashboard.test"})
    ).status_code == 200


async def test_login_rate_limit(client):
    for _ in range(5):
        assert (
            await client.post("/api/login", json={"password": "wrong"})
        ).status_code == 401
    assert (
        await client.post("/api/login", json={"password": PASSWORD})
    ).status_code == 429


async def test_account_list_never_returns_credentials(client, accounts):
    await login(client)
    response = await client.get("/api/overview")
    assert len(response.json()["accounts"]) == 3
    assert "secret" not in response.text
    assert "SESSDATA" not in response.text
    assert response.json()["accounts"][0]["health"]["status"] == "unknown"


async def test_history_filter_search_and_pagination(
    client, engine, accounts, post_factory
):
    tg, x, _ = accounts
    for i in range(3):
        post = post_factory(str(i), text="hello " + str(i))
        job = ensure_job(engine, tg.id, x.id, [post])
        start_job(engine, job["id"], [post])
        finish_job(
            engine, job["id"], error="failed" if i == 0 else "", dest_ids=[str(i + 100)]
        )
    await login(client)
    response = await client.get("/api/jobs?status=failed")
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["text"] == "hello 0"
    response = await client.get("/api/jobs?search=hello%202")
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["links"] == ["https://x.com/i/status/102"]
    assert (await client.get("/api/jobs?page_size=2")).json()["total"] == 3
    assert len((await client.get("/api/jobs?page=2&page_size=2")).json()["items"]) == 1
    assert (await client.get("/api/jobs?status=bad")).status_code == 400
    assert (await client.get("/api/jobs?page=0")).status_code == 422


@respx.mock
async def test_manual_retry_is_queued_once_and_sends_only_failed_target(
    client, runtime, accounts, post_factory
):
    tg, x, b = accounts
    post = post_factory("42", text="a queued message")
    failed = ensure_job(runtime.engine, tg.id, x.id, [post])
    finish_job(runtime.engine, failed["id"], error="temporary error")
    succeeded = ensure_job(runtime.engine, tg.id, b.id, [post])
    finish_job(runtime.engine, succeeded["id"], dest_ids=["200"])
    await login(client)
    assert (await client.post(f"/api/jobs/{succeeded['id']}/retry")).status_code == 409
    assert (await client.post(f"/api/jobs/{failed['id']}/retry")).status_code == 202
    assert runtime.wake.is_set()
    assert (await client.post(f"/api/jobs/{failed['id']}/retry")).status_code == 409
    route = respx.post("https://api.x.com/2/tweets").respond(
        201, json={"data": {"id": "300"}}
    )
    assert await runtime.cycle() == 1
    assert route.call_count == 1
    assert get_job(runtime.engine, failed["id"])["status"] == "succeeded"
    assert get_job(runtime.engine, succeeded["id"])["attempts"] == 0


@respx.mock
async def test_manual_retry_preserves_successful_parts(
    client, runtime, accounts, post_factory
):
    tg, x, _ = accounts
    post = post_factory("partial", text="中" * 200)
    route = respx.post("https://api.x.com/2/tweets").mock(
        side_effect=[
            httpx.Response(201, json={"data": {"id": "111"}}),
            httpx.Response(503),
            httpx.Response(201, json={"data": {"id": "222"}}),
        ]
    )
    with pytest.raises(RuntimeError):
        await publish_tracked(runtime.engine, tg, x, [post], [post])
    job = ensure_job(runtime.engine, tg.id, x.id, [post])
    await login(client)
    assert (await client.post(f"/api/jobs/{job['id']}/retry")).status_code == 202
    await runtime.cycle()
    assert route.call_count == 3
    assert json.loads(route.calls[-1].request.content)["reply"] == {
        "in_reply_to_tweet_id": "111"
    }
    final = get_job(runtime.engine, job["id"])
    assert final["status"] == "succeeded" and final["attempts"] == 2


async def test_failed_tasks_and_errors_are_visible_and_redacted(
    client, runtime, accounts, post_factory, monkeypatch
):
    tg, x, _ = accounts
    monkeypatch.setattr(
        "apis.twitter.publish_outbound",
        AsyncMock(
            side_effect=RuntimeError("failed x-secret-token and tg-secret-token")
        ),
    )
    post = post_factory(text="payload")
    with pytest.raises(RuntimeError):
        await publish_tracked(runtime.engine, tg, x, [post], [post])
    await login(client)
    response = await client.get("/api/jobs?status=failed")
    assert response.json()["total"] == 1
    assert "secret-token" not in response.text
    assert "已隐藏" in response.text


@pytest.mark.parametrize(
    "code,is_login,expected",
    [
        (0, True, "healthy"),
        (0, False, "expired"),
        (-101, False, "expired"),
        (-412, False, "error"),
    ],
)
@respx.mock
async def test_bilibili_cookie_health(client, accounts, code, is_login, expected):
    _, _, b = accounts
    respx.get("https://api.bilibili.com/x/web-interface/nav").respond(
        200, json={"code": code, "data": {"isLogin": is_login, "mid": 456}}
    )
    await login(client)
    response = await client.post(f"/api/accounts/{b.id}/check")
    assert response.json()["health"]["status"] == expected
    assert response.json()["token_expires_at"] is None
    assert "bili-secret-cookie" not in response.text


@respx.mock
async def test_network_error_does_not_claim_cookie_expired(client, accounts):
    b = accounts[2]
    respx.get("https://api.bilibili.com/x/web-interface/nav").mock(
        side_effect=httpx.ConnectTimeout("failed")
    )
    await login(client)
    result = (await client.post(f"/api/accounts/{b.id}/check")).json()
    assert result["health"]["status"] == "error"
    assert "暂时无法判断" in result["health"]["detail"]


@respx.mock
async def test_telegram_check_validates_channel_and_webhook(client, accounts):
    tg = accounts[0]
    respx.get("https://api.telegram.org/bottg-secret-token/getMe").respond(
        200, json={"ok": True, "result": {"id": 123}}
    )
    respx.get("https://api.telegram.org/bottg-secret-token/getChatMember").respond(
        200, json={"ok": True, "result": {"status": "administrator"}}
    )
    respx.get("https://api.telegram.org/bottg-secret-token/getWebhookInfo").respond(
        200, json={"ok": True, "result": {"url": "https://old-handler.test"}}
    )
    await login(client)
    result = (await client.post(f"/api/accounts/{tg.id}/check")).json()
    assert (
        result["health"]["status"] == "error"
        and "webhook" in result["health"]["detail"]
    )


@respx.mock
async def test_x_check_refreshes_expired_access_token(client, runtime, accounts):
    x = accounts[1]
    set_credentials(
        runtime.engine,
        x.id,
        {"client_id": "client", "refresh_token": "refresh", "token_expires_at": "1"},
    )
    respx.post("https://api.x.com/2/oauth2/token").respond(
        200,
        json={
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        },
    )
    respx.get("https://api.x.com/2/users/me").respond(200, json={"data": {"id": "123"}})
    await login(client)
    result = (await client.post(f"/api/accounts/{x.id}/check")).json()
    assert result["health"]["status"] == "healthy" and result["auto_refresh"]
    assert result["token_expires_at"] > time.time()
    assert get_all_credentials(runtime.engine, x.id)["refresh_token"] == "new-refresh"


def test_interrupted_jobs_are_recoverable(engine, accounts, post_factory):
    tg, x, _ = accounts
    post = post_factory()
    job = ensure_job(engine, tg.id, x.id, [post])
    start_job(engine, job["id"], [post])
    SyncRuntime(engine, RoutingConfig())
    assert get_job(engine, job["id"])["status"] == "failed"


def test_restart_reconciles_an_already_recorded_success(engine, accounts, post_factory):
    from db.sync_state import mark_synced

    tg, x, _ = accounts
    post = post_factory()
    job = ensure_job(engine, tg.id, x.id, [post])
    start_job(engine, job["id"], [post])
    mark_synced(engine, tg.id, post.id, x.id, "12345")
    SyncRuntime(engine, RoutingConfig())
    recovered = get_job(engine, job["id"])
    assert recovered["status"] == "succeeded"
    assert json.loads(recovered["dest_post_ids"]) == ["12345"]


@respx.mock
async def test_expired_cookie_from_publish_updates_health(
    runtime, accounts, post_factory
):
    tg, _, b = accounts
    respx.post("https://api.bilibili.com/x/dynamic/feed/create/dyn").respond(
        200, json={"code": -101}
    )
    with pytest.raises(RuntimeError):
        await publish_tracked(runtime.engine, tg, b, [post_factory()], [post_factory()])
    assert get_health(runtime.engine, b.id)["status"] == "expired"
