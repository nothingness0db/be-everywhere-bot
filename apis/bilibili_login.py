"""Bilibili QR login, ported from bili-sync's MIT-licensed login flow.

See THIRD_PARTY_NOTICES.md. Cookie values stay on the server.
"""

from urllib.parse import urlparse

import httpx

from apis.bilibili import _client, _response_data

PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode"


async def generate_qr() -> dict[str, str]:
    async with _client({}) as client:
        data = _response_data(await client.get(f"{PASSPORT}/generate", timeout=15))
    url, key = data.get("url"), data.get("qrcode_key")
    if (
        not isinstance(url, str)
        or urlparse(url).scheme != "https"
        or urlparse(url).hostname not in {"passport.bilibili.com", "account.bilibili.com"}
        or not isinstance(key, str)
        or not key
    ):
        raise ValueError("B 站未返回有效的登录二维码，请重新获取。")
    return {"url": url, "key": key}


async def poll_qr(key: str) -> dict:
    async with _client({}) as client:
        response = await client.get(
            f"{PASSPORT}/poll", params={"qrcode_key": key}, timeout=15
        )
        data = _response_data(response)
        code = data.get("code")
        if code in (86101, 86090):
            return {"status": "pending", "scanned": code == 86090}
        if code == 86038:
            return {"status": "expired"}
        if code != 0:
            raise ValueError("B 站登录状态异常，请重新获取二维码。")
        creds = {
            cookie.name: cookie.value
            for cookie in response.cookies.jar
            if cookie.name in {"SESSDATA", "bili_jct", "DedeUserID", "buvid3"}
        }
        if not all(creds.get(k) for k in ("SESSDATA", "bili_jct", "DedeUserID")):
            raise ValueError("B 站未返回完整的登录凭证，请重新扫码。")
        creds["bili_refresh_token"] = data.get("refresh_token", "")
        if not creds.get("buvid3"):
            # The fingerprint endpoint can fail independently of a valid login.
            try:
                fingerprint = _response_data(
                    await client.get("/x/web-frontend/getbuvid", timeout=10)
                )
                creds["buvid3"] = fingerprint.get("buvid", "")
            except (httpx.HTTPError, RuntimeError, ValueError):
                pass
    return {"status": "success", "credentials": creds}


async def verify_credentials(creds: dict[str, str]) -> dict:
    async with _client(creds) as client:
        data = _response_data(await client.get("/x/web-interface/nav", timeout=15))
    if not data.get("isLogin") or not data.get("mid"):
        raise ValueError("B 站登录已失效，请重新扫码或更新 Cookie。")
    if creds.get("DedeUserID") and creds["DedeUserID"] != str(data["mid"]):
        raise ValueError("B 站返回的账号信息不一致，请重新扫码。")
    return data
