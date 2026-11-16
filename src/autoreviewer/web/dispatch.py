"""Run work after the webhook has been answered (GitHub gives up after 10 s).

In-process for M0, where the only job is a check run. DBOS replaces this behind the same `submit`
once reviews exist, because a review must survive a restart; a greeting doesn't need to.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable

logger = logging.getLogger("autoreviewer.dispatch")


class Dispatcher:
    def __init__(self, *, max_concurrent: int = 4, remember_deliveries: int = 10_000) -> None:
        self._slots = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task[None]] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._remember = remember_deliveries

    def first_time(self, delivery_id: str) -> bool:
        """Record a delivery id; False if it was already seen (GitHub redeliveries reuse the id)."""
        if delivery_id in self._seen:
            return False
        self._seen[delivery_id] = None
        if len(self._seen) > self._remember:
            self._seen.popitem(last=False)
        return True

    def submit(self, name: str, job: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(self._run(name, job), name=name)
        self._tasks.add(task)  # keep a reference, or the task can be garbage-collected mid-flight
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for everything submitted so far (shutdown, and tests)."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, name: str, job: Callable[[], Awaitable[None]]) -> None:
        async with self._slots:
            try:
                await job()
            except Exception:
                logger.exception("job %s failed", name)
