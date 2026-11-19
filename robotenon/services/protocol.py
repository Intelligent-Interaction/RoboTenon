from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from ..schemas import (
    Action,
    ActionChunk,
    BodyExecutionResult,
    EnvironmentCommand,
    EnvironmentFeedback,
    EnvironmentHealth,
    MonitorReport,
)


class EnvironmentService(Protocol):
    """Resident observation/command service for one environment endpoint.

    Workflow and Monitor receive separate clients implementing this protocol.
    A client may be an in-process wrapper or an RPC/UDS proxy; neither caller
    needs to know how the simulator, RoboLab or RoboDojo is hosted.
    """

    async def start(self) -> None: ...

    async def reset(self, context: Any) -> EnvironmentFeedback: ...

    async def observe(self) -> EnvironmentFeedback: ...

    async def command(self, command: EnvironmentCommand) -> EnvironmentFeedback: ...

    async def health(self) -> EnvironmentHealth: ...

    async def close(self) -> None: ...


class BodyService(Protocol):
    """Resident cerebellum/body service receiving action chunks."""

    async def start(self) -> None: ...

    async def execute(self, action_chunk: ActionChunk) -> BodyExecutionResult: ...

    async def interrupt(
        self,
        reason: str,
        *,
        safe_action: ActionChunk | None = None,
    ) -> BodyExecutionResult: ...

    async def health(self) -> EnvironmentHealth: ...

    async def close(self) -> None: ...


class MonitorService(Protocol):
    """Compliance service independent from Workflow; supports check/request."""

    async def start(self) -> None: ...

    async def check(
        self,
        feedback: EnvironmentFeedback | None = None,
        *,
        context: Any | None = None,
    ) -> MonitorReport: ...

    async def request(
        self,
        name: str | None = None,
        request: Mapping[str, Any] | None = None,
        *,
        feedback: EnvironmentFeedback | None = None,
        context: Any | None = None,
    ) -> MonitorReport: ...

    async def latest(self) -> MonitorReport | None: ...

    async def health(self) -> EnvironmentHealth: ...

    async def close(self) -> None: ...


BodyExecutor = Callable[[Action], Any | Awaitable[Any]]
BodyStopper = Callable[[str], Any | Awaitable[Any]]
MonitorChecker = Callable[[EnvironmentFeedback, Any | None], MonitorReport | Any | Awaitable[Any]]
MonitorRequester = Callable[..., MonitorReport | Any | Awaitable[Any]]


__all__ = [
    "BodyExecutor",
    "BodyService",
    "BodyStopper",
    "EnvironmentService",
    "MonitorChecker",
    "MonitorRequester",
    "MonitorService",
]
