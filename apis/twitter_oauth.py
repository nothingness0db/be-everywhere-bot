"""X user authorization with PKCE and rotating refresh tokens."""

import base64
import hashlib
import secrets
import time
from getpass import getpass
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from sqlalchemy.engine import Engine

from db.accounts import set_credentials

TOKEN_URL = "https://api.x.com/2/oauth2/token"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"


async def exchange_token(creds: dict[str, str], data: dict[str, str]) -> dict[str, str]:
    data = {**data, "client_id": creds["client_id"]}
    auth = None
    if creds.get("client_secret"):
        auth = httpx.BasicAuth(creds["client_id"], creds["client_secret"])
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(TOKEN_URL, data=data, auth=auth)
    if not response.is_success:
        raise RuntimeError(
            f"X OAuth failed (HTTP {response.status_code}); run --auth=twitter again"
        )
    token = response.json()
    if not token.get("access_token"):
        raise RuntimeError("X OAuth response did not contain an access token")
    return {
        "bearer_token": token["access_token"],
        "refresh_token": token.get("refresh_token") or creds.get("refresh_token", ""),
        "token_expires_at": str(time.time() + float(token.get("expires_in", 7200))),
    }


def callback_code(callback_url: str, expected_state: str, redirect_uri: str) -> str:
    url = urlsplit(callback_url)
    redirect = urlsplit(redirect_uri)
    if (url.scheme, url.netloc, url.path) != (
        redirect.scheme,
        redirect.netloc,
        redirect.path,
    ):
        raise ValueError("Callback URL does not match the configured redirect URI")
    query = parse_qs(url.query)
    if not secrets.compare_digest(query.get("state", [""])[0], expected_state):
        raise ValueError("X OAuth state does not match; restart --auth=twitter")
    if query.get("error") or not query.get("code"):
        raise ValueError("X authorization was denied or returned no code")
    return query["code"][0]


async def authorize() -> dict[str, str]:
    client_id = input("X OAuth 2.0 Client ID: ").strip()
    client_secret = getpass(
        "Client Secret (confidential apps only; otherwise leave blank): "
    ).strip()
    redirect_uri = (
        input("Registered callback URL [http://127.0.0.1:8765/callback]: ").strip()
        or "http://127.0.0.1:8765/callback"
    )
    if not client_id:
        raise ValueError("X OAuth Client ID is required")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    print("Open this URL in your browser and authorize your own account:\n")
    print("https://x.com/i/oauth2/authorize?" + urlencode(params))
    print("\nAfter authorization, copy the FULL callback URL from the address bar.")
    print(
        "The local callback page may show a connection error; its URL still contains the code."
    )
    callback = getpass("Full callback URL (hidden; paste promptly): ").strip()
    code = callback_code(callback, state, redirect_uri)
    creds = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    creds.update(
        await exchange_token(
            creds,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
    )
    if not creds["refresh_token"]:
        raise RuntimeError(
            "X did not grant offline access; re-authorize with offline.access"
        )
    return creds


async def refresh_if_needed(
    engine: Engine, account_id: int, creds: dict[str, str]
) -> dict[str, str]:
    expires = creds.get("token_expires_at")
    if not expires or float(expires) > time.time() + 120:
        return creds
    if not creds.get("refresh_token") or not creds.get("client_id"):
        raise RuntimeError("X user token expired; run --auth=twitter again")
    refreshed = await exchange_token(
        creds, {"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]}
    )
    set_credentials(engine, account_id, refreshed)
    return {**creds, **refreshed}
