"""Checkpoint each published part so retries can resume a partially sent thread."""

import hashlib
import json
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from apis.types import OutboundPost, PublishResult
from db.schema import outbound_deliveries


def delivery_key(outbound: OutboundPost, index: int) -> str:
    payload = json.dumps([index, asdict(outbound)], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def get_delivery(
    engine: Engine, source_id: int, dest_id: int, key: str
) -> PublishResult | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(outbound_deliveries).where(
                outbound_deliveries.c.source_account_id == source_id,
                outbound_deliveries.c.dest_account_id == dest_id,
                outbound_deliveries.c.delivery_key == key,
            )
        ).first()
    return PublishResult(post_id=row.post_id, reply_ref=row.reply_ref) if row else None


def save_delivery(
    engine: Engine, source_id: int, dest_id: int, key: str, result: PublishResult
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(outbound_deliveries)
            .values(
                source_account_id=source_id,
                dest_account_id=dest_id,
                delivery_key=key,
                post_id=result.post_id,
                reply_ref=result.reply_ref,
            )
            .on_conflict_do_nothing()
        )
