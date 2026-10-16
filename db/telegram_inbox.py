import json
import time

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from db.schema import account_credentials, telegram_inbox


def save_page(
    engine: Engine,
    messages: list[tuple[int, dict]],
    account_ids: list[int],
    offset: int,
) -> None:
    """Commit the full page and its shared-bot offsets in a single transaction."""
    with engine.begin() as conn:
        for account_id, message in messages:
            payload = json.dumps(message, ensure_ascii=False, sort_keys=True)
            stmt = insert(telegram_inbox).values(
                account_id=account_id,
                message_id=str(message["message_id"]),
                payload=payload,
                received_at=time.time(),
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["account_id", "message_id"],
                    set_={"payload": payload, "received_at": time.time()},
                    where=telegram_inbox.c.payload != payload,
                )
            )
        for account_id in account_ids:
            stmt = insert(account_credentials).values(
                account_id=account_id,
                key="update_offset",
                value=str(offset),
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["account_id", "key"],
                    set_={"value": str(offset)},
                )
            )


def read_messages(
    engine: Engine, account_id: int, settle_seconds: float = 2
) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(telegram_inbox).where(
                telegram_inbox.c.account_id == account_id,
            )
        ).all()
    messages = [(json.loads(row.payload), row.received_at) for row in rows]
    group_times: dict[str, float] = {}
    for message, received_at in messages:
        if group_id := message.get("media_group_id"):
            group_times[group_id] = max(group_times.get(group_id, 0), received_at)
    cutoff = time.time() - settle_seconds
    return [
        message
        for message, _ in messages
        if not message.get("media_group_id")
        or group_times[message["media_group_id"]] <= cutoff
    ]
