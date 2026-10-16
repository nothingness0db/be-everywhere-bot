"""Publish text/image dynamics using the account's Bilibili web session.

Request shapes: https://docs.rs/bpi-rs/latest/src/bpi_rs/dynamic/publish.rs.html
These are web endpoints; expired sessions and verification challenges are errors.
"""

from datetime import datetime
from getpass import getpass

import httpx
from sqlalchemy.engine import Engine

from apis.types import MediaItem, OutboundPost, Post, PublishResult
from config import NETWORK_BILIBILI
from db.accounts import (
    Account,
    create_account,
    find_account,
    get_all_credentials,
    set_credentials,
    update_remote_id,
)

API_BASE = "https://api.bilibili.com"
AUTH_HELP = """\
bilibili (destination only):
  Prompts for SESSDATA, bili_jct, and optional buvid3 from your own logged-in
  browser's bilibili.com cookies. Publishes text/image dynamics, not video uploads.
  Cookies are stored locally in SQLite. Re-run --auth=bilibili if they expire.
"""


def _client(creds: dict[str, str]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API_BASE,
        timeout=120,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://t.bilibili.com/",
            "Origin": "https://t.bilibili.com",
        },
        cookies={
            key: creds[key]
            for key in ("SESSDATA", "bili_jct", "buvid3", "DedeUserID")
            if creds.get(key)
        },
    )


def _response_data(response: httpx.Response) -> dict:
    # Do not include request URLs (csrf query), headers, or cookies in exceptions.
    if not response.is_success:
        raise RuntimeError(f"Bilibili request failed (HTTP {response.status_code})")
    body = response.json()
    code = body.get("code")
    if code != 0:
        if code in (-101, -111):
            raise RuntimeError(
                "Bilibili session/CSRF expired; run --auth=bilibili again"
            )
        raise RuntimeError(
            f"Bilibili API error {code}: {body.get('message', 'unknown error')}"
        )
    return body.get("data") or {}


async def authenticate(engine: Engine, label: str = "default") -> Account:
    print(AUTH_HELP)
    creds = {
        "SESSDATA": getpass("SESSDATA (hidden): ").strip(),
        "bili_jct": getpass("bili_jct (hidden): ").strip(),
        "buvid3": getpass("buvid3 (optional, hidden): ").strip(),
    }
    if not creds["SESSDATA"] or not creds["bili_jct"]:
        raise RuntimeError("SESSDATA and bili_jct are required")
    async with _client(creds) as client:
        user = _response_data(await client.get("/x/web-interface/nav"))
    if not user.get("isLogin") or not user.get("mid"):
        raise RuntimeError("Bilibili session is not logged in; check your cookies")
    remote_id = str(user["mid"])
    creds.update(DedeUserID=remote_id, username=user.get("uname", remote_id))
    account = find_account(engine, NETWORK_BILIBILI, label)
    if account:
        update_remote_id(engine, account.id, remote_id)
    else:
        account = create_account(engine, NETWORK_BILIBILI, label, remote_id)
    set_credentials(engine, account.id, creds)
    print(f"Bilibili account '{label}' configured: {creds['username']} ({remote_id})")
    return account


async def fetch_posts(
    engine: Engine,
    account_id: int,
    since: datetime | None = None,
    include_replies: bool = True,
    max_pages: int | None = None,
) -> list[Post]:
    return []  # Destination only: no reverse sync.


async def download_media(media: MediaItem, engine: Engine, account_id: int) -> bytes:
    raise NotImplementedError("Bilibili is a destination only")


async def publish_outbound(
    engine: Engine,
    account_id: int,
    outbound: OutboundPost,
    media_bytes: list[bytes] | None = None,
    *,
    reply_to: str | None = None,
) -> PublishResult:
    creds = get_all_credentials(engine, account_id)
    if not creds.get("SESSDATA") or not creds.get("bili_jct"):
        raise RuntimeError("Bilibili credentials missing; run --auth=bilibili")
    raw_media = media_bytes or []
    if any(item.media_type != "photo" for item in outbound.media):
        raise ValueError(
            "Bilibili dynamics support photos only; video/GIF posting is not supported"
        )
    if len(raw_media) != len(outbound.media):
        raise ValueError("Bilibili media count does not match downloaded attachments")
    if not outbound.text.strip() and not outbound.media:
        raise ValueError("Cannot publish an empty Bilibili dynamic")

    async with _client(creds) as client:
        pictures = []
        for index, raw in enumerate(raw_media):
            image = _response_data(
                await client.post(
                    "/x/dynamic/feed/draw/upload_bfs",
                    data={
                        "csrf": creds["bili_jct"],
                        "category": "daily",
                        "biz": "new_dyn",
                    },
                    files={"file_up": (f"photo{index}.jpg", raw, "image/jpeg")},
                )
            )
            pictures.append(
                {
                    "img_src": image["image_url"],
                    "img_width": image["image_width"],
                    "img_height": image["image_height"],
                    "img_size": len(raw) / 1024,
                }
            )
        dynamic = {
            "content": {
                "contents": [{"raw_text": outbound.text, "type": 1, "biz_id": ""}]
            },
            "scene": 2 if pictures else 1,
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
        }
        if pictures:
            dynamic["pics"] = pictures
        data = _response_data(
            await client.post(
                "/x/dynamic/feed/create/dyn",
                params={"csrf": creds["bili_jct"]},
                json={"dyn_req": dynamic},
            )
        )
    post_id = data.get("dyn_id_str") or data.get("dyn_id")
    if not post_id:
        raise RuntimeError("Bilibili create dynamic response is missing its ID")
    # Dynamic continuations are separate dynamics; there is no thread/reply protocol here.
    return PublishResult(post_id=str(post_id))
