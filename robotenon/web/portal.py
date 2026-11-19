from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..infra.events import EventBus
from ..infra.visualization import LiveGateway, LiveSnapshot
from ..schemas import EventEnvelope


class PortalService:
    """Framework-level Web Portal boundary, independent of HTTP libraries.

    A production adapter may expose these methods through FastAPI/SSE. Keeping
    the service dependency-free makes the control runtime installable on a
    robot computer while the actual UI remains a separate process.
    """

    def __init__(self, live: LiveGateway):
        self.live = live
        self.events: EventBus = live.event_bus

    def snapshot(self, **kwargs: Any) -> LiveSnapshot:
        return self.live.snapshot(**kwargs)

    def grafana_url(self, base_url: str, dashboard_uid: str, *, variables: dict[str, str] | None = None) -> str:
        query = {f"var-{key}": value for key, value in (variables or {}).items()}
        query_text = "&".join(f"{key}={value}" for key, value in query.items())
        return f"{base_url.rstrip('/')}/d/{dashboard_uid}" + (f"?{query_text}" if query_text else "")

    async def sse_events(self, *, capacity: int = 256) -> AsyncIterator[EventEnvelope]:
        _, iterator = await self.events.subscribe(capacity=capacity)
        async for event in iterator:
            yield event
