"""Persistent settings shared by the CLI and the running dashboard."""

import json
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from db.schema import runtime_settings
from sync.routing import RoutingConfig, parse_routing


def effective_routing(engine: Engine, fallback: RoutingConfig) -> RoutingConfig:
    with engine.connect() as conn:
        value = conn.execute(
            select(runtime_settings.c.value).where(runtime_settings.c.key == "routing")
        ).scalar_one_or_none()
    return parse_routing(json.loads(value)) if value is not None else fallback


def save_routing(engine: Engine, routing: RoutingConfig) -> None:
    value = json.dumps(asdict(routing), ensure_ascii=False)
    stmt = insert(runtime_settings).values(key="routing", value=value)
    with engine.begin() as conn:
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["key"], set_={"value": stmt.excluded.value}
            )
        )
