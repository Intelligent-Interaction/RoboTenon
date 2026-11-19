"""Contracts shared by resident and request-driven monitor plugins."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..schemas import EnvironmentFeedback, MonitorReport, SafetyLevel

MonitorMode = Literal["resident", "request"]


@dataclass(slots=True)
class MonitorContext:
    """Ephemeral runtime view supplied to a monitor invocation.

    ``graph_context`` is the live workflow ``GraphContext`` when a request
    originates from a graph.  ``environment`` is the already configured
    EnvironmentService client (normally the process singleton proxy); a
    monitor must not create a second environment connection.  This object is
    deliberately not persisted.
    """

    environment: Any = None
    safety_state: Any = None
    params: Any = None
    graph_context: Any = None
    monitor_id: str | None = None
    request: Mapping[str, Any] | None = None
    # Ephemeral decoded frame view injected by MonitorRuntime.  Feedback and
    # GraphContext continue to carry only URI metadata.
    observation_view: Any = None

    @property
    def ctx(self) -> Any:
        """Short alias for the originating GraphContext, when present."""

        return self.graph_context

    @property
    def observation(self) -> Any:
        return self.observation_view


MonitorValue = MonitorReport | SafetyLevel | Mapping[str, Any] | bool


class Monitor(Protocol):
    """Plugin contract used by :class:`MonitorRuntime`.

    The same implementation may be configured with both ``resident`` and
    ``request`` modes.  Resident invocations receive the latest environment
    feedback on a timer; request invocations additionally receive the caller's
    request payload.  Returning a P3/P4 report publishes a safety signal and
    lets the existing Body watchdog/Workflow emergency path react.
    """

    async def check(
        self,
        feedback: EnvironmentFeedback | None = None,
        *,
        request: Mapping[str, Any] | None = None,
        context: MonitorContext | None = None,
    ) -> MonitorValue: ...


__all__ = ["Monitor", "MonitorContext", "MonitorMode", "MonitorValue"]
