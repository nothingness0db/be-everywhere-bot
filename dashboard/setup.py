"""Account onboarding and live routing settings for the authenticated UI."""

import asyncio
import base64
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Annotated

import httpx
import qrcode
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from qrcode.image.svg import SvgPathImage

from apis import bilibili_login, telegram
from db.accounts import (
    Account,
    create_account,
    find_account,
    get_all_credentials,
    list_accounts,
    set_credentials,
    update_remote_id,
)
from db.health import set_health
from db.settings import save_routing
from sync.routing import parse_routing
from sync.runtime import SyncRuntime

Label = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
QR_SECONDS = 180
POLL_SECONDS = 2


class AccountLabel(BaseModel):
    label: Label = "main"


class TelegramSettings(AccountLabel):
    bot_token: SecretStr | None = None
    channel_id: str = Field(min_length=1, max_length=128)


class BilibiliCookie(AccountLabel):
    sessdata: SecretStr
    bili_jct: SecretStr
    buvid3: SecretStr | None = None


class RoutingSettings(BaseModel):
    prefix: str = Field(min_length=1, max_length=32)
    poll_interval_seconds: float = Field(ge=1, le=3600, allow_inf_nan=False)
    album_settle_seconds: float = Field(ge=0, le=60, allow_inf_nan=False)
    forward: dict[str, list[str]] = Field(default_factory=dict, max_length=32)


@dataclass
class Challenge:
    owner: str
    label: str
    created_at: float = field(default_factory=time.monotonic)
    key: str = ""
    last_poll: float = 0
    credentials: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _secret(value: SecretStr | None) -> str:
    result = value.get_secret_value().strip() if value else ""
    if len(result) > 8192 or any(c in result for c in "\r\n\x00"):
        raise HTTPException(400, "凭证格式有误，请检查后重试。")
    return result


def _owner(request: Request) -> str:
    return request.state.session["csrf"]


async def _tg_call(client: httpx.AsyncClient, token: str, method: str, **params):
    response = await client.get(telegram._api_url(token, method), params=params)
    if not response.is_success or not response.json().get("ok"):
        raise HTTPException(
            400, f"TG {method} 失败，请检查 Bot Token、频道标识和管理员权限。"
        )
    return response.json()["result"]


async def connect_telegram(runtime: SyncRuntime, body: TelegramSettings) -> Account:
    async with runtime.lock:
        existing = find_account(runtime.engine, "telegram", body.label)
        old = get_all_credentials(runtime.engine, existing.id) if existing else {}
        token = _secret(body.bot_token) or old.get("bot_token", "")
        channel = body.channel_id.strip()
        if not token or not channel:
            raise HTTPException(400, "首次连接需要填写 Bot Token 和频道标识。")
        async with httpx.AsyncClient(timeout=15) as client:
            bot = await _tg_call(client, token, "getMe")
            chat = await _tg_call(client, token, "getChat", chat_id=channel)
            if chat.get("type") != "channel":
                raise HTTPException(400, "请选择 TG 频道，目前不支持群组或私聊。")
            member = await _tg_call(
                client, token, "getChatMember", chat_id=chat["id"], user_id=bot["id"]
            )
            if member.get("status") not in {"administrator", "creator"}:
                raise HTTPException(400, "请先把 Bot 加入频道并设为管理员。")
            hook = await _tg_call(client, token, "getWebhookInfo")
            if hook.get("url"):
                raise HTTPException(
                    409, "此 Bot 已设置 webhook，请先停用原 webhook 再连接。"
                )
        if existing and not telegram._chat_matches(
            old.get("channel_id", existing.remote_id), chat
        ):
            raise HTTPException(
                409, "这个账号名称已绑定另一个频道。连接新频道时请使用新名称。"
            )
        for peer in list_accounts(runtime.engine, "telegram"):
            peer_creds = get_all_credentials(runtime.engine, peer.id)
            if (
                peer.id != (existing.id if existing else None)
                and peer_creds.get("bot_token") == token
                and telegram._chat_matches(
                    peer_creds.get("channel_id", peer.remote_id), chat
                )
            ):
                raise HTTPException(
                    409, "此 Bot 和频道已连接，请使用已有账号名称更新。"
                )
        same_bot = old.get("bot_token") == token or old.get("bot_id") == str(bot["id"])
        creds = {
            "bot_token": token,
            "bot_id": str(bot["id"]),
            "bot_username": bot.get("username", ""),
            "channel_id": str(chat["id"]),
            "channel_title": chat.get("title", channel),
            "channel_username": chat.get("username", ""),
            "update_offset": old.get("update_offset", "0") if same_bot else "0",
        }
        account = existing or create_account(
            runtime.engine, "telegram", body.label, str(chat["id"])
        )
        update_remote_id(runtime.engine, account.id, str(chat["id"]))
        set_credentials(runtime.engine, account.id, creds)
        set_health(
            runtime.engine,
            account.id,
            "healthy",
            "Bot Token 有效，频道管理员权限正常，轮询可用。",
        )
        runtime.wake.set()
        return account


async def connect_bilibili(
    runtime: SyncRuntime, label: str, creds: dict, active=None
) -> Account:
    async with runtime.lock:
        user = await bilibili_login.verify_credentials(creds)
        if active is not None and not active():
            raise HTTPException(410, "二维码已取消或过期，请重新获取。")
        remote_id = str(user["mid"])
        existing = find_account(runtime.engine, "bilibili", label)
        if existing and existing.remote_id != remote_id:
            raise HTTPException(
                409, "这个账号名称已绑定另一个 B 站账号，请使用新名称连接。"
            )
        account = existing or create_account(
            runtime.engine, "bilibili", label, remote_id
        )
        set_credentials(
            runtime.engine,
            account.id,
            {
                "SESSDATA": creds["SESSDATA"],
                "bili_jct": creds["bili_jct"],
                "buvid3": creds.get("buvid3", ""),
                "DedeUserID": remote_id,
                "username": user.get("uname", remote_id),
                "bili_refresh_token": creds.get("bili_refresh_token", ""),
            },
        )
        set_health(
            runtime.engine,
            account.id,
            "healthy",
            "Cookie 当前有效；失效后可在此页面重新扫码登录。",
        )
        runtime.wake.set()
        return account


def register_setup(app: FastAPI, runtime: SyncRuntime, account_view) -> None:
    challenges: dict[str, Challenge] = {}

    def cleanup():
        for challenge_id, challenge in list(challenges.items()):
            if time.monotonic() - challenge.created_at >= QR_SECONDS:
                challenges.pop(challenge_id, None)

    # Never return upstream exception URLs, cookies, or form values to the UI.
    def failed(exc: Exception):
        if isinstance(exc, HTTPException):
            raise exc
        if isinstance(exc, httpx.HTTPError):
            raise HTTPException(502, "连接平台超时或网络异常，请稍后重试。") from None
        if isinstance(exc, ValueError):
            raise HTTPException(
                400, "登录信息验证失败，请重新扫码或检查填写内容。"
            ) from None
        if isinstance(exc, RuntimeError):
            raise HTTPException(
                400, "平台未接受登录凭证，请重新扫码或检查 Cookie。"
            ) from None
        raise exc

    @app.get("/api/settings")
    async def settings():
        telegram_accounts = []
        bilibili_accounts = []
        for account in list_accounts(runtime.engine):
            creds = get_all_credentials(runtime.engine, account.id)
            if account.network == "telegram":
                telegram_accounts.append(
                    {
                        "label": account.label,
                        "channel_id": creds.get("channel_id", ""),
                        "channel_title": creds.get("channel_title", ""),
                        "bot_username": creds.get("bot_username", ""),
                        "has_token": bool(creds.get("bot_token")),
                    }
                )
            elif account.network == "bilibili":
                bilibili_accounts.append(account_view(runtime, account))
        return {
            "telegram": telegram_accounts,
            "bilibili": bilibili_accounts,
            "routing": asdict(runtime.routing),
        }

    @app.post("/api/settings/telegram")
    async def telegram_settings(body: TelegramSettings):
        try:
            account = await connect_telegram(runtime, body)
        except (httpx.HTTPError, ValueError, RuntimeError, HTTPException) as exc:
            failed(exc)
        return account_view(runtime, account)

    @app.post("/api/settings/bilibili/cookie")
    async def cookie_settings(body: BilibiliCookie):
        try:
            creds = {
                "SESSDATA": _secret(body.sessdata),
                "bili_jct": _secret(body.bili_jct),
                "buvid3": _secret(body.buvid3),
            }
            if not creds["SESSDATA"] or not creds["bili_jct"]:
                raise HTTPException(400, "请填写 SESSDATA 和 bili_jct。")
            account = await connect_bilibili(runtime, body.label, creds)
        except (httpx.HTTPError, ValueError, RuntimeError, HTTPException) as exc:
            failed(exc)
        return account_view(runtime, account)

    @app.post("/api/settings/routing")
    async def routing_settings(body: RoutingSettings):
        telegram_labels = {
            account.label for account in list_accounts(runtime.engine, "telegram")
        }
        forward = {}
        for source, targets in body.forward.items():
            if not targets or not set(targets) <= telegram_labels:
                raise HTTPException(
                    400, "转发目标必须是已连接的 TG 频道账号名称。"
                )
            forward[source] = targets
        try:
            routing = parse_routing(
                {
                    "mode": "telegram_prefix",
                    "prefixes": {body.prefix: ["bilibili"]},
                    "poll_interval_seconds": body.poll_interval_seconds,
                    "album_settle_seconds": body.album_settle_seconds,
                    "forward": forward,
                }
            )
        except ValueError:
            raise HTTPException(
                400, "前缀需要是一个完整词，不能包含空格或换行。"
            ) from None
        async with runtime.lock:
            save_routing(runtime.engine, routing)
            runtime.routing = routing
            runtime.wake.set()
        return {"routing": asdict(routing)}

    @app.post("/api/settings/bilibili/qr")
    async def generate(body: AccountLabel, request: Request):
        cleanup()
        owner = _owner(request)
        owned = [
            (key, value) for key, value in challenges.items() if value.owner == owner
        ]
        if any(time.monotonic() - value.created_at < 3 for _, value in owned):
            raise HTTPException(429, "请稍等几秒再刷新二维码。")
        for key, _ in owned:
            challenges.pop(key, None)
        if len(challenges) >= 64:
            raise HTTPException(429, "当前登录请求较多，请稍后重试。")
        challenge_id = secrets.token_urlsafe(24)
        challenge = Challenge(owner, body.label)
        challenges[challenge_id] = challenge
        try:
            result = await bilibili_login.generate_qr()
            challenge.key = result["key"]
            qr = qrcode.make(result["url"], image_factory=SvgPathImage, border=4)
            image = (
                "data:image/svg+xml;base64," + base64.b64encode(qr.to_string()).decode()
            )
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            challenges.pop(challenge_id, None)
            failed(exc)
        return {"id": challenge_id, "image": image, "expires_in": QR_SECONDS}

    @app.post("/api/settings/bilibili/qr/{challenge_id}/poll")
    async def poll(challenge_id: str, request: Request):
        cleanup()
        challenge = challenges.get(challenge_id)
        if not challenge or challenge.owner != _owner(request):
            raise HTTPException(404, "二维码已过期，请重新获取。")
        async with challenge.lock:
            if time.monotonic() - challenge.last_poll < POLL_SECONDS:
                raise HTTPException(429, "正在等待扫码，请稍后再检测。")
            challenge.last_poll = time.monotonic()
            try:
                if challenge.credentials is None:
                    result = await bilibili_login.poll_qr(challenge.key)
                    if result["status"] != "success":
                        if result["status"] == "expired":
                            challenges.pop(challenge_id, None)
                        return result
                    challenge.credentials = result["credentials"]
                account = await connect_bilibili(
                    runtime,
                    challenge.label,
                    challenge.credentials,
                    active=lambda: (
                        challenges.get(challenge_id) is challenge
                        and time.monotonic() - challenge.created_at < QR_SECONDS
                    ),
                )
            except (httpx.HTTPError, ValueError, RuntimeError, HTTPException) as exc:
                failed(exc)
            challenges.pop(challenge_id, None)
            return {"status": "success", "account": account_view(runtime, account)}

    @app.delete("/api/settings/bilibili/qr/{challenge_id}")
    async def cancel(challenge_id: str, request: Request):
        challenge = challenges.get(challenge_id)
        if challenge and challenge.owner == _owner(request):
            challenges.pop(challenge_id, None)
        return {"ok": True}
