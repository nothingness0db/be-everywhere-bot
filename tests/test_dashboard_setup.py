import asyncio
import base64
import httpx
import pytest
import respx

from dashboard.app import create_app
from db.accounts import find_account, get_all_credentials, set_credentials
from sync.routing import RoutingConfig
from sync.runtime import SyncRuntime

PASSWORD = "dashboard-setup-test-password"
NAV = "https://api.bilibili.com/x/web-interface/nav"
QR = "https://passport.bilibili.com/x/passport-login/web/qrcode"
TOKEN = "12345:private-token-value"
TG = f"https://api.telegram.org/bot{TOKEN}"


@pytest.fixture
def runtime(engine):
    return SyncRuntime(engine, RoutingConfig())


@pytest.fixture
async def client(runtime):
    app = create_app(runtime, PASSWORD)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test"
    ) as client:
        await client.post("/api/login", json={"password": PASSWORD})
        client.headers["X-CSRF-Token"] = (await client.get("/api/session")).json()[
            "csrf_token"
        ]
        yield client


def mock_nav(mid=456):
    return respx.get(NAV).respond(
        200,
        json={"code": 0, "data": {"isLogin": True, "mid": mid, "uname": "测试账号"}},
    )


def mock_qr():
    return respx.get(QR + "/generate").respond(
        200,
        json={
            "code": 0,
            "data": {
                "url": "https://account.bilibili.com/h5-app/passport/login/scan?auth_code=scan-secret",
                "qrcode_key": "private-qr-key",
            },
        },
    )


def qr_success():
    return httpx.Response(
        200,
        json={"code": 0, "data": {"code": 0, "refresh_token": "private-refresh"}},
        headers=[
            (
                "set-cookie",
                "SESSDATA=private-session; Domain=.bilibili.com; Path=/; HttpOnly",
            ),
            ("set-cookie", "bili_jct=private-csrf; Domain=.bilibili.com; Path=/"),
            ("set-cookie", "DedeUserID=456; Domain=.bilibili.com; Path=/"),
            ("set-cookie", "buvid3=private-fingerprint; Domain=.bilibili.com; Path=/"),
        ],
    )


def mock_telegram(*, webhook="", member="administrator", chat_id=-100123):
    respx.get(TG + "/getMe").respond(
        200, json={"ok": True, "result": {"id": 12345, "username": "mybot"}}
    )
    respx.get(TG + "/getChat").respond(
        200,
        json={
            "ok": True,
            "result": {
                "id": chat_id,
                "type": "channel",
                "username": "news",
                "title": "我的频道",
            },
        },
    )
    respx.get(TG + "/getChatMember").respond(
        200, json={"ok": True, "result": {"status": member}}
    )
    respx.get(TG + "/getWebhookInfo").respond(
        200, json={"ok": True, "result": {"url": webhook}}
    )


async def test_settings_require_login_and_csrf(runtime, client):
    async with httpx.AsyncClient(
        transport=client._transport, base_url="http://dashboard.test"
    ) as anonymous:
        assert (await anonymous.get("/settings")).status_code == 303
        assert (await anonymous.get("/api/settings")).status_code == 401
        assert (
            await anonymous.post("/api/settings/bilibili/qr", json={})
        ).status_code == 401
    assert (await client.get("/settings")).status_code == 200
    assert (
        await client.post(
            "/api/settings/bilibili/qr", json={}, headers={"X-CSRF-Token": ""}
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/settings/routing", json={}, headers={"Origin": "https://foreign.test"}
        )
    ).status_code == 403


@respx.mock
async def test_cookie_login_is_verified_and_never_echoed(runtime, client):
    mock_nav()
    response = await client.post(
        "/api/settings/bilibili/cookie",
        json={
            "label": "main",
            "sessdata": "private-session",
            "bili_jct": "private-csrf",
            "buvid3": "private-fingerprint",
        },
    )
    assert response.status_code == 200
    assert response.json()["name"] == "测试账号"
    assert response.json()["health"]["status"] == "healthy"
    account = find_account(runtime.engine, "bilibili", "main")
    assert (
        get_all_credentials(runtime.engine, account.id)["SESSDATA"] == "private-session"
    )
    assert runtime.wake.is_set()
    for path in ["/api/settings", "/api/overview"]:
        response = await client.get(path)
        assert "private-session" not in response.text
        assert "private-csrf" not in response.text
        assert "private-fingerprint" not in response.text
    assert (await client.get("/api/settings")).json()["bilibili"][0]["label"] == "main"


@respx.mock
async def test_bad_login_or_another_identity_does_not_overwrite_credentials(
    runtime, client
):
    route = mock_nav()
    body = {"label": "main", "sessdata": "original-cookie", "bili_jct": "original-csrf"}
    assert (
        await client.post("/api/settings/bilibili/cookie", json=body)
    ).status_code == 200
    route.respond(200, json={"code": -101})
    assert (
        await client.post(
            "/api/settings/bilibili/cookie", json={**body, "sessdata": "bad-cookie"}
        )
    ).status_code == 400
    route.respond(200, json={"code": 0, "data": {"isLogin": True, "mid": 789}})
    assert (
        await client.post(
            "/api/settings/bilibili/cookie", json={**body, "sessdata": "other-cookie"}
        )
    ).status_code == 409
    account = find_account(runtime.engine, "bilibili", "main")
    assert account.remote_id == "456"
    assert (
        get_all_credentials(runtime.engine, account.id)["SESSDATA"] == "original-cookie"
    )


@respx.mock
async def test_telegram_setup_checks_channel_and_preserves_offset_on_update(
    runtime, client
):
    mock_telegram()
    response = await client.post(
        "/api/settings/telegram",
        json={"label": "main", "bot_token": TOKEN, "channel_id": "@news"},
    )
    assert response.status_code == 200 and response.json()["name"] == "我的频道"
    account = find_account(runtime.engine, "telegram", "main")
    set_credentials(runtime.engine, account.id, {"update_offset": "118"})
    # Updating with a blank token keeps the saved token and the inbox checkpoint.
    assert (
        await client.post(
            "/api/settings/telegram",
            json={"label": "main", "bot_token": None, "channel_id": "-100123"},
        )
    ).status_code == 200
    creds = get_all_credentials(runtime.engine, account.id)
    assert creds["update_offset"] == "118" and creds["bot_token"] == TOKEN
    response = await client.get("/api/settings")
    assert TOKEN not in response.text
    assert response.json()["telegram"][0]["has_token"] is True
    assert response.json()["telegram"][0]["channel_id"] == "-100123"
    respx.get(TG + "/getChat").respond(
        200,
        json={
            "ok": True,
            "result": {"id": -100999, "type": "channel", "username": "other"},
        },
    )
    assert (
        await client.post(
            "/api/settings/telegram", json={"label": "main", "channel_id": "@other"}
        )
    ).status_code == 409
    assert get_all_credentials(runtime.engine, account.id)["channel_id"] == "-100123"


@pytest.mark.parametrize(
    "webhook,member,expected",
    [("https://old.test/webhook", "administrator", 409), ("", "member", 400)],
)
@respx.mock
async def test_telegram_rejects_webhook_or_missing_admin(
    runtime, client, webhook, member, expected
):
    mock_telegram(webhook=webhook, member=member)
    response = await client.post(
        "/api/settings/telegram", json={"bot_token": TOKEN, "channel_id": "@news"}
    )
    assert response.status_code == expected
    assert find_account(runtime.engine, "telegram", "main") is None


@respx.mock
async def test_secrets_are_redacted_from_network_and_validation_errors(client):
    respx.get(TG + "/getMe").mock(side_effect=httpx.ConnectError("failed URL " + TG))
    response = await client.post(
        "/api/settings/telegram", json={"bot_token": TOKEN, "channel_id": "@news"}
    )
    assert response.status_code == 502 and TOKEN not in response.text
    response = await client.post(
        "/api/settings/bilibili/cookie",
        json={
            "sessdata": {"token": "secret-invalid-object"},
            "bili_jct": "secret-csrf",
        },
    )
    assert response.status_code == 422 and "secret-" not in response.text


async def test_routing_updates_immediately_and_survives_new_runtime(runtime, client):
    response = await client.post(
        "/api/settings/routing",
        json={
            "prefix": "BILI",
            "poll_interval_seconds": 1.5,
            "album_settle_seconds": 0.5,
        },
    )
    assert response.status_code == 200
    assert runtime.routing.prefixes == {"bili": ("bilibili",)}
    assert runtime.wake.is_set()
    restarted = SyncRuntime(runtime.engine, RoutingConfig())
    assert restarted.routing == runtime.routing
    assert (await client.get("/api/overview")).json()["routing"]["prefixes"] == {
        "bili": ["bilibili"]
    }
    before = runtime.routing
    for values in [
        {"prefix": "x b"},
        {"poll_interval_seconds": 0},
        {"album_settle_seconds": -1},
    ]:
        body = {
            "prefix": "b",
            "poll_interval_seconds": 5,
            "album_settle_seconds": 2,
            **values,
        }
        assert (await client.post("/api/settings/routing", json=body)).status_code in (
            400,
            422,
        )
        assert runtime.routing == before


async def test_routing_saves_forward_targets(runtime, client):
    from db.accounts import create_account

    create_account(runtime.engine, "telegram", "main", "@news")
    create_account(runtime.engine, "telegram", "mirror", "@mirror")
    body = {
        "prefix": "b",
        "poll_interval_seconds": 5,
        "album_settle_seconds": 2,
    }
    response = await client.post(
        "/api/settings/routing", json={**body, "forward": {"main": ["mirror"]}}
    )
    assert response.status_code == 200
    assert runtime.routing.forward == {"main": ("mirror",)}
    restarted = SyncRuntime(runtime.engine, RoutingConfig())
    assert restarted.routing.forward == {"main": ("mirror",)}
    settings = (await client.get("/api/settings")).json()
    assert settings["routing"]["forward"] == {"main": ["mirror"]}

    bad = await client.post(
        "/api/settings/routing", json={**body, "forward": {"main": ["ghost"]}}
    )
    assert bad.status_code == 400
    assert runtime.routing.forward == {"main": ("mirror",)}
    empty = await client.post("/api/settings/routing", json=body)
    assert empty.status_code == 200
    assert runtime.routing.forward == {}


@respx.mock
async def test_qr_login_pending_scanned_success_and_private_cookie_storage(
    runtime, client, monkeypatch
):
    monkeypatch.setattr("dashboard.setup.POLL_SECONDS", 0)
    mock_qr()
    mock_nav()
    poll = respx.get(QR + "/poll").mock(
        side_effect=[
            httpx.Response(200, json={"code": 0, "data": {"code": 86101}}),
            httpx.Response(200, json={"code": 0, "data": {"code": 86090}}),
            qr_success(),
        ]
    )
    response = await client.post("/api/settings/bilibili/qr", json={"label": "main"})
    assert response.status_code == 200 and "private-qr-key" not in response.text
    qr = response.json()
    assert b"<svg" in base64.b64decode(qr["image"].split(",", 1)[1])
    path = "/api/settings/bilibili/qr/" + qr["id"] + "/poll"
    assert (await client.post(path)).json() == {"status": "pending", "scanned": False}
    assert (await client.post(path)).json() == {"status": "pending", "scanned": True}
    response = await client.post(path)
    assert response.status_code == 200 and response.json()["status"] == "success"
    assert "private-" not in response.text
    assert poll.call_count == 3
    account = find_account(runtime.engine, "bilibili", "main")
    creds = get_all_credentials(runtime.engine, account.id)
    assert (
        creds["SESSDATA"] == "private-session"
        and creds["bili_refresh_token"] == "private-refresh"
    )
    assert "private-" not in (await client.get("/api/settings")).text


@respx.mock
async def test_qr_is_bound_to_login_session_and_cancel_stops_it(runtime, client):
    mock_qr()
    qr = (await client.post("/api/settings/bilibili/qr", json={})).json()
    path = "/api/settings/bilibili/qr/" + qr["id"]
    async with httpx.AsyncClient(
        transport=client._transport, base_url="http://dashboard.test"
    ) as stranger:
        await stranger.post("/api/login", json={"password": PASSWORD})
        stranger.headers["X-CSRF-Token"] = (await stranger.get("/api/session")).json()[
            "csrf_token"
        ]
        assert (await stranger.post(path + "/poll")).status_code == 404
        await stranger.delete(path)
    assert (await client.delete(path)).status_code == 200
    assert (await client.post(path + "/poll")).status_code == 404
    assert find_account(runtime.engine, "bilibili", "main") is None


@respx.mock
async def test_qr_expiry_and_poll_rate_limit(client, monkeypatch):
    mock_qr()
    respx.get(QR + "/poll").respond(200, json={"code": 0, "data": {"code": 86101}})
    qr = (await client.post("/api/settings/bilibili/qr", json={})).json()
    path = "/api/settings/bilibili/qr/" + qr["id"] + "/poll"
    assert (await client.post(path)).status_code == 200
    assert (await client.post(path)).status_code == 429
    monkeypatch.setattr("dashboard.setup.QR_SECONDS", -1)
    assert (await client.post(path)).status_code == 404


@respx.mock
async def test_successful_qr_cookies_survive_temporary_nav_error(
    runtime, client, monkeypatch
):
    monkeypatch.setattr("dashboard.setup.POLL_SECONDS", 0)
    mock_qr()
    poll = respx.get(QR + "/poll").mock(return_value=qr_success())
    nav = respx.get(NAV).mock(side_effect=httpx.ConnectTimeout("temporary error"))
    qr = (await client.post("/api/settings/bilibili/qr", json={})).json()
    path = "/api/settings/bilibili/qr/" + qr["id"] + "/poll"
    assert (await client.post(path)).status_code == 502
    nav.respond(200, json={"code": 0, "data": {"isLogin": True, "mid": 456}})
    assert (await client.post(path)).json()["status"] == "success"
    assert poll.call_count == 1
    account = find_account(runtime.engine, "bilibili", "main")
    assert (
        get_all_credentials(runtime.engine, account.id)["SESSDATA"] == "private-session"
    )


@respx.mock
async def test_cancelled_inflight_qr_does_not_save_account(
    runtime, client, monkeypatch
):
    mock_qr()
    mock_nav()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_poll(key):
        entered.set()
        await release.wait()
        return {
            "status": "success",
            "credentials": {
                "SESSDATA": "private-session",
                "bili_jct": "private-csrf",
                "DedeUserID": "456",
            },
        }

    monkeypatch.setattr("apis.bilibili_login.poll_qr", slow_poll)
    qr = (await client.post("/api/settings/bilibili/qr", json={})).json()
    path = "/api/settings/bilibili/qr/" + qr["id"]
    task = asyncio.create_task(client.post(path + "/poll"))
    await entered.wait()
    await client.delete(path)
    release.set()
    assert (await task).status_code == 410
    assert find_account(runtime.engine, "bilibili", "main") is None
