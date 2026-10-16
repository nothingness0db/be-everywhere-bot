import time

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from db.accounts import get_all_credentials
from db.schema import account_health

_SECRET_KEYS = {
    "bot_token",
    "bearer_token",
    "access_token",
    "refresh_token",
    "client_secret",
    "SESSDATA",
    "bili_jct",
    "buvid3",
    "app_secret",
    "access_jwt",
    "refresh_jwt",
    "bili_refresh_token",
}


def credential_error_status(message: str) -> str:
    lowered = message.lower()
    return (
        "expired"
        if any(
            term in lowered for term in ("expired", "unauthorized", "(401)", "http 401")
        )
        else "error"
    )


def safe_error(engine: Engine, error: Exception, *account_ids: int) -> str:
    message = str(error) or type(error).__name__
    for account_id in account_ids:
        for key, value in get_all_credentials(engine, account_id).items():
            if key in _SECRET_KEYS and value:
                message = message.replace(value, "[已隐藏]")
    return message[:1500]


def set_health(engine: Engine, account_id: int, status: str, detail: str) -> None:
    stmt = insert(account_health).values(
        account_id=account_id, status=status, detail=detail, checked_at=time.time()
    )
    with engine.begin() as conn:
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["account_id"],
                set_={
                    "status": stmt.excluded.status,
                    "detail": stmt.excluded.detail,
                    "checked_at": stmt.excluded.checked_at,
                },
            )
        )


def get_health(engine: Engine, account_id: int) -> dict:
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(account_health).where(account_health.c.account_id == account_id)
            )
            .mappings()
            .first()
        )
    return (
        dict(row)
        if row
        else {"status": "unknown", "detail": "尚未检测登录状态", "checked_at": None}
    )
