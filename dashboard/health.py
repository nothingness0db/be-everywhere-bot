"""Read-only account probes. No test posts are published."""

import httpx

from apis import bilibili, telegram, twitter
from apis.twitter_oauth import refresh_if_needed
from db.accounts import Account, get_all_credentials
from db.health import get_health, safe_error, set_health
from sync.runtime import SyncRuntime


async def check_account(runtime: SyncRuntime, account: Account) -> dict:
    async with runtime.lock:
        engine = runtime.engine
        creds = get_all_credentials(engine, account.id)
        status, detail = "healthy", "登录检测通过；发布权限以实际同步结果为准。"
        try:
            if account.network == "bilibili":
                if not creds.get("SESSDATA") or not creds.get("bili_jct"):
                    status, detail = (
                        "unconfigured",
                        "缺少 SESSDATA 或 bili_jct，请在服务器重新配置账号。",
                    )
                else:
                    async with bilibili._client(creds) as client:
                        response = await client.get("/x/web-interface/nav", timeout=15)
                    if response.status_code != 200:
                        status, detail = (
                            "error",
                            f"B 站连接失败（HTTP {response.status_code}），暂时无法判断 Cookie 是否有效。",
                        )
                    else:
                        body = response.json()
                        if body.get("code") == -101 or (
                            body.get("code") == 0
                            and not (body.get("data") or {}).get("isLogin")
                        ):
                            status, detail = (
                                "expired",
                                "B 站 Cookie 已失效，请在服务器重新运行 --auth=bilibili。",
                            )
                        elif body.get("code") != 0:
                            status, detail = (
                                "error",
                                f"B 站检测失败（code {body.get('code')}），暂时无法判断 Cookie 是否有效。",
                            )
                        elif str(body["data"].get("mid")) != account.remote_id:
                            status, detail = (
                                "error",
                                "当前 Cookie 对应另一个 B 站账号，请重新配置。",
                            )
                        else:
                            detail = "Cookie 当前有效；网页接口不提供可确认的失效日期。"
            elif account.network == "twitter":
                if not creds.get("bearer_token"):
                    status, detail = (
                        "unconfigured",
                        "缺少 X 用户授权，请运行 --auth=twitter。",
                    )
                else:
                    creds = await refresh_if_needed(engine, account.id, creds)
                    async with httpx.AsyncClient(timeout=15) as client:
                        response = await client.get(
                            f"{twitter.TWITTER_APP.api_base_url}/users/me",
                            headers={
                                "Authorization": f"Bearer {creds['bearer_token']}"
                            },
                        )
                    if response.status_code == 401:
                        status, detail = (
                            "expired",
                            "X 用户授权已失效，请重新运行 --auth=twitter。",
                        )
                    elif response.status_code != 200:
                        status, detail = (
                            "error",
                            f"X 检测失败（HTTP {response.status_code}），请检查 API 权限或额度。",
                        )
                    elif (
                        str((response.json().get("data") or {}).get("id"))
                        != account.remote_id
                    ):
                        status, detail = "error", "X 授权对应的账号与已配置账号不一致。"
            elif account.network == "telegram":
                token, channel = await telegram._get_bot_credentials(engine, account.id)
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(telegram._api_url(token, "getMe"))
                    if response.status_code == 401:
                        status, detail = (
                            "expired",
                            "TG Bot Token 已失效，请重新配置账号。",
                        )
                    else:
                        telegram._check_response(response, "getMe")
                        bot_id = response.json()["result"]["id"]
                        response = await client.get(
                            telegram._api_url(token, "getChatMember"),
                            params={"chat_id": channel, "user_id": bot_id},
                        )
                        telegram._check_response(response, "getChatMember")
                        member = response.json()["result"]
                        if member.get("status") not in {"administrator", "creator"}:
                            status, detail = (
                                "error",
                                "Bot 尚未成为频道管理员，请检查频道权限。",
                            )
                        else:
                            response = await client.get(
                                telegram._api_url(token, "getWebhookInfo")
                            )
                            telegram._check_response(response, "getWebhookInfo")
                            if response.json()["result"].get("url"):
                                status, detail = (
                                    "error",
                                    "Bot 当前设置了 webhook，与频道轮询冲突，请先停用原 webhook。",
                                )
                            else:
                                detail = (
                                    "Bot Token 有效，频道管理员权限正常，轮询可用。"
                                )
            else:
                status, detail = (
                    "unknown",
                    "此平台暂不支持主动检测，可查看最近的同步结果。",
                )
        except httpx.RequestError:
            status, detail = "error", "连接超时或网络异常，暂时无法判断凭证是否有效。"
        except Exception as exc:
            status, detail = "error", safe_error(engine, exc, account.id)
        set_health(engine, account.id, status, detail)
        return get_health(engine, account.id)
