from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any

from ..schemas import EventEnvelope
from .events import EventBus
from .metrics import MetricsDaemon
from .trace import TraceDaemon


@dataclass(frozen=True, slots=True)
class LiveSnapshot:
    events: tuple[EventEnvelope, ...]
    metrics: tuple[Any, ...]
    traces: tuple[Any, ...]


class LiveGateway:
    """Bounded live view for the Web Portal; dashboards cannot back-pressure Workflow."""

    def __init__(self, *, event_bus: EventBus | None = None, metrics: MetricsDaemon | None = None,
                 traces: TraceDaemon | None = None, event_capacity: int = 2048):
        self.event_bus = event_bus or EventBus()
        self.metrics = metrics
        self.traces = traces
        self._events: deque[EventEnvelope] = deque(maxlen=event_capacity)
        self._lock = asyncio.Lock()

    def accept(self, event: EventEnvelope) -> None:
        self._events.append(event)
        self.event_bus.publish(event)

    def snapshot(self, *, metric_name: str | None = None, limit: int = 200) -> LiveSnapshot:
        events = tuple(list(self._events)[-limit:])
        metrics = tuple(self.metrics.snapshot(name=metric_name, limit=limit)) if self.metrics else ()
        traces = tuple(self.traces.snapshot(limit=limit)) if self.traces else ()
        return LiveSnapshot(events=events, metrics=metrics, traces=traces)

    def grafana_variables(self, *, run_id: str | None = None, environment_id: str | None = None,
                          task_type: str | None = None, metric_group: str | None = None,
                          action_type: str | None = None, scope: str | None = None,
                          avg_window: str | None = None, from_ts: str | None = None,
                          to_ts: str | None = None, dimensions: dict[str, str] | None = None) -> dict[str, str]:
        values = {"run_id": run_id, "environment": environment_id, "task_type": task_type,
                  "metric_group": metric_group, "action_type": action_type, "scope": scope,
                  "avg_window": avg_window, "from": from_ts, "to": to_ts}
        values.update(dimensions or {})
        return {key: value for key, value in values.items() if value is not None}
