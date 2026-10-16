"""One worker serializes automatic publishing, manual retries and token checks."""

import asyncio
import time

from sqlalchemy.engine import Engine

from db.jobs import queued_jobs, recover_interrupted_jobs
from db.settings import effective_routing
from sync.engine import retry_job, run_sync
from sync.routing import RoutingConfig


class SyncRuntime:
    def __init__(self, engine: Engine, routing: RoutingConfig):
        self.engine = engine
        self.routing = effective_routing(engine, routing)
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.started_at = time.time()
        self.last_cycle_at: float | None = None
        self.worker_running = False
        recover_interrupted_jobs(engine)

    async def cycle(self, *, enforce_min_age: bool = True) -> int:
        async with self.lock:
            queued = queued_jobs(self.engine)
            if queued:
                count = 0
                for job in queued:
                    count += await retry_job(self.engine, job["id"])
            else:
                count = await run_sync(
                    self.engine, routing=self.routing, enforce_min_age=enforce_min_age
                )
            self.last_cycle_at = time.time()
            return count
