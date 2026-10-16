"""Authenticated dashboard and JSON API, served by the publishing process."""

import json
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dashboard.health import check_account
from dashboard.setup import register_setup
from db.accounts import (
    account_display_name,
    get_account,
    get_all_credentials,
    list_accounts,
)
from db.health import get_health
from db.jobs import deserialize_posts, get_job, job_counts, list_jobs, queue_retry
from sync.runtime import SyncRuntime

STATIC = Path(__file__).parent / "static"
COOKIE = "sync_dashboard"
SESSION_SECONDS = 12 * 3600


class Login(BaseModel):
    password: str = Field(max_length=1024)


def _account_view(runtime: SyncRuntime, account) -> dict:
    creds = get_all_credentials(runtime.engine, account.id)
    try:
        expires_at = (
            float(creds["token_expires_at"]) if creds.get("token_expires_at") else None
        )
    except ValueError:
        expires_at = None
    return {
        "id": account.id,
        "network": account.network,
        "label": account.label,
        "name": account_display_name(account, runtime.engine),
        "health": get_health(runtime.engine, account.id),
        "token_expires_at": expires_at,
        "auto_refresh": bool(creds.get("refresh_token")),
    }


def _job_view(runtime: SyncRuntime, job: dict) -> dict:
    source = get_account(runtime.engine, job["source_account_id"])
    dest = get_account(runtime.engine, job["dest_account_id"])
    posts = deserialize_posts(job["payload"])

    dest_ids = json.loads(job["dest_post_ids"])
    links = []
    if dest and dest.network in {"twitter", "bilibili"}:
        base = (
            "https://x.com/i/status/"
            if dest.network == "twitter"
            else "https://t.bilibili.com/"
        )
        links = [base + pid for pid in dest_ids if pid.isdigit()]
    return {
        "id": job["id"],
        "status": job["status"],
        "attempts": job["attempts"],
        "error": job["error"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "text": "\n\n".join(p.text for p in posts if p.text),
        "media_count": sum(len(p.media) for p in posts),
        "source_post_ids": [p.id for p in posts],
        "source": {
            "network": source.network,
            "name": account_display_name(source, runtime.engine),
        }
        if source
        else None,
        "destination": {
            "network": dest.network,
            "name": account_display_name(dest, runtime.engine),
            "label": dest.label,
        }
        if dest
        else None,
        "links": links,
    }


def create_app(runtime: SyncRuntime, password: str) -> FastAPI:
    if len(password) < 12:
        raise ValueError(
            "Set DASHBOARD_PASSWORD to at least 12 characters before enabling the dashboard."
        )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sessions: dict[str, dict] = {}
    failures: dict[str, list[float]] = {}

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Validation errors can otherwise echo tokens and cookies from a request.
        return JSONResponse(
            {"detail": "提交内容不符合要求，请检查输入格式和数值范围。"},
            status_code=422,
        )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        path = request.url.path
        origin = request.headers.get("origin")
        session = sessions.get(request.cookies.get(COOKIE, ""))
        if session and session["expires"] <= time.time():
            session = None
        public = path in {"/login", "/api/login"} or path.startswith("/assets/")
        if (
            request.method not in {"GET", "HEAD"}
            and origin
            and origin != str(request.base_url).rstrip("/")
        ):
            response = JSONResponse({"detail": "不允许跨站操作。"}, status_code=403)
        elif not public and session is None:
            response = (
                JSONResponse({"detail": "请先登录。"}, status_code=401)
                if path.startswith("/api/")
                else RedirectResponse("/login", status_code=303)
            )
        elif (
            not public
            and request.method not in {"GET", "HEAD"}
            and not secrets.compare_digest(
                request.headers.get("x-csrf-token", ""), session["csrf"]
            )
        ):
            response = JSONResponse(
                {"detail": "页面已过期，请刷新后重试。"}, status_code=403
            )
        else:
            request.state.session = session
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get("/login")
    async def login_page():
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        now = time.time()
        peer = request.client.host if request.client else "unknown"
        attempts = [stamp for stamp in failures.get(peer, []) if stamp > now - 300]
        if len(attempts) >= 5:
            raise HTTPException(429, "尝试次数过多，请 5 分钟后再试。")
        if not secrets.compare_digest(body.password.encode(), password.encode()):
            if len(failures) > 1024:
                failures.clear()
            failures[peer] = attempts + [now]
            raise HTTPException(401, "密码不正确。")
        failures.pop(peer, None)
        for token in list(sessions):
            if sessions[token]["expires"] < now:
                sessions.pop(token)
        if len(sessions) >= 64:
            sessions.pop(next(iter(sessions)))
        # Rotate a pre-existing login so its old session cannot remain active.
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        token = secrets.token_urlsafe(32)
        sessions[token] = {
            "csrf": secrets.token_urlsafe(32),
            "expires": now + SESSION_SECONDS,
        }
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=SESSION_SECONDS,
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/settings")
    async def settings_page():
        return FileResponse(STATIC / "settings.html")

    @app.get("/api/session")
    async def session(request: Request):
        return {"csrf_token": request.state.session["csrf"]}

    @app.get("/api/overview")
    async def overview():
        return {
            "accounts": [
                _account_view(runtime, a) for a in list_accounts(runtime.engine)
            ],
            "counts": job_counts(runtime.engine),
            "worker": {
                "running": runtime.worker_running,
                "busy": runtime.lock.locked(),
                "last_cycle_at": runtime.last_cycle_at,
                "started_at": runtime.started_at,
            },
            "routing": {
                "mode": runtime.routing.mode,
                "prefixes": runtime.routing.prefixes,
            },
        }

    @app.post("/api/accounts/{account_id}/check")
    async def account_check(account_id: int):
        account = get_account(runtime.engine, account_id)
        if account is None:
            raise HTTPException(404, "账号不存在。")
        await check_account(runtime, account)
        return _account_view(runtime, account)

    @app.get("/api/jobs")
    async def jobs(
        status: str = "",
        search: str = Query("", max_length=200),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        if status not in {"", "pending", "failed", "succeeded"}:
            raise HTTPException(400, "不支持的状态筛选。")
        rows, count = list_jobs(
            runtime.engine, status=status, search=search, page=page, page_size=page_size
        )
        return {
            "items": [_job_view(runtime, row) for row in rows],
            "total": count,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: int):
        job = get_job(runtime.engine, job_id)
        if job is None:
            raise HTTPException(404, "同步记录不存在。")
        return _job_view(runtime, job)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: int):
        if get_job(runtime.engine, job_id) is None:
            raise HTTPException(404, "同步记录不存在。")
        if not queue_retry(runtime.engine, job_id):
            raise HTTPException(
                409, "只有失败的记录可以重试，任务可能已在处理或已成功。"
            )
        runtime.wake.set()
        return {"ok": True, "status": "queued"}

    register_setup(app, runtime, _account_view)
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app
