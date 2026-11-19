from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ..schemas import EventEnvelope


class EventBus:
    """Best-effort fan-out for telemetry; slow observers never block Workflow."""

    def __init__(self, *, default_capacity: int = 256) -> None:
        self.default_capacity = default_capacity
        self._subscribers: dict[int, asyncio.Queue[EventEnvelope]] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def subscribe(self, *, capacity: int | None = None) -> tuple[int, AsyncIterator[EventEnvelope]]:
        async with self._lock:
            subscriber_id = self._next_id
            self._next_id += 1
            self._subscribers[subscriber_id] = asyncio.Queue(maxsize=capacity or self.default_capacity)

        async def iterator() -> AsyncIterator[EventEnvelope]:
            queue = self._subscribers[subscriber_id]
            try:
                while True:
                    yield await queue.get()
            finally:
                self._subscribers.pop(subscriber_id, None)

        return subscriber_id, iterator()

    def publish(self, event: EventEnvelope) -> int:
        delivered = 0
        for queue in tuple(self._subscribers.values()):
            try:
                queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                    delivered += 1
                except asyncio.QueueEmpty:
                    pass
        return delivered

    def subscriber_count(self) -> int:
        return len(self._subscribers)
