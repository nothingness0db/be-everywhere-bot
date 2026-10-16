"""Durable publish status, message snapshots and manual retry requests."""

import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from apis.types import MediaItem, Post
from db.schema import sync_jobs


def serialize_posts(posts: list[Post]) -> str:
    return json.dumps(
        [{**asdict(p), "created_at": p.created_at.isoformat()} for p in posts],
        ensure_ascii=False,
    )


def deserialize_posts(payload: str) -> list[Post]:
    posts = []
    for raw in json.loads(payload):
        raw["created_at"] = datetime.fromisoformat(raw["created_at"])
        raw["media"] = [MediaItem(**item) for item in raw["media"]]
        posts.append(Post(**raw))
    return posts


def ensure_job(engine: Engine, source_id: int, dest_id: int, posts: list[Post]) -> dict:
    key = hashlib.sha256(
        json.dumps([source_id, dest_id, [p.id for p in posts]]).encode()
    ).hexdigest()
    now = time.time()
    with engine.begin() as conn:
        conn.execute(
            insert(sync_jobs)
            .values(
                job_key=key,
                source_account_id=source_id,
                dest_account_id=dest_id,
                payload=serialize_posts(posts),
                status="pending",
                attempts=0,
                error="",
                dest_post_ids="[]",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["job_key"])
        )
        row = (
            conn.execute(select(sync_jobs).where(sync_jobs.c.job_key == key))
            .mappings()
            .one()
        )
    return dict(row)


def start_job(engine: Engine, job_id: int, posts: list[Post]) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(sync_jobs)
            .where(sync_jobs.c.id == job_id)
            .values(
                status="running",
                payload=serialize_posts(posts),
                attempts=sync_jobs.c.attempts + 1,
                error="",
                updated_at=time.time(),
            )
        )


def finish_job(
    engine: Engine, job_id: int, *, error: str = "", dest_ids: list[str] | None = None
) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(sync_jobs)
            .where(sync_jobs.c.id == job_id)
            .values(
                status="failed" if error else "succeeded",
                error=error,
                dest_post_ids=json.dumps(dest_ids or []),
                updated_at=time.time(),
            )
        )


def get_job(engine: Engine, job_id: int) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(select(sync_jobs).where(sync_jobs.c.id == job_id))
            .mappings()
            .first()
        )
    return dict(row) if row else None


def queue_retry(engine: Engine, job_id: int) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            update(sync_jobs)
            .where(
                sync_jobs.c.id == job_id,
                sync_jobs.c.status == "failed",
            )
            .values(status="queued", updated_at=time.time())
        )
    return result.rowcount == 1


def queued_jobs(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                select(sync_jobs)
                .where(
                    sync_jobs.c.status == "queued",
                )
                .order_by(sync_jobs.c.updated_at)
            ).mappings()
        ]


def recover_interrupted_jobs(engine: Engine) -> None:
    from db.sync_state import get_dest_post_id

    with engine.connect() as conn:
        interrupted = list(
            conn.execute(
                select(sync_jobs).where(sync_jobs.c.status == "running")
            ).mappings()
        )
    for job in interrupted:
        ids = [
            get_dest_post_id(
                engine, job["source_account_id"], p.id, job["dest_account_id"]
            )
            for p in deserialize_posts(job["payload"])
        ]
        if ids and all(ids):
            finish_job(engine, job["id"], dest_ids=list(dict.fromkeys(ids)))
        else:
            finish_job(engine, job["id"], error="程序在上次发送期间停止，可重试继续。")


def list_jobs(
    engine: Engine,
    *,
    status: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    conditions = []
    if status == "pending":
        conditions.append(sync_jobs.c.status.in_(["pending", "queued", "running"]))
    elif status:
        conditions.append(sync_jobs.c.status == status)
    if search:
        conditions.append(sync_jobs.c.payload.contains(search, autoescape=True))
    with engine.connect() as conn:
        count = conn.scalar(
            select(func.count()).select_from(sync_jobs).where(*conditions)
        )
        rows = conn.execute(
            select(sync_jobs)
            .where(*conditions)
            .order_by(
                sync_jobs.c.updated_at.desc(),
                sync_jobs.c.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).mappings()
        return [dict(row) for row in rows], count


def job_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                select(sync_jobs.c.status, func.count()).group_by(sync_jobs.c.status)
            ).all()
        )
