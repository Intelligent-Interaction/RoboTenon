"""Abstract Body service contract used by official Body providers.

The :class:`~robotenon.services.protocol.BodyService` protocol remains the
structural extension contract for remote or third-party implementations.
``BaseBodyService`` is the convenience base class for providers maintained in
RoboTenon; it makes the required lifecycle and action-control methods visible to
type checkers and fails at class construction time when an official provider
forgets one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..schemas import ActionChunk, BodyExecutionResult, EnvironmentHealth


class BaseBodyService(ABC):
    """Common contract for the resident action-execution boundary."""

    @abstractmethod
    async def start(self) -> None:
        """Acquire driver resources and start safety/watchdog tasks."""

    @abstractmethod
    async def execute(self, action_chunk: ActionChunk) -> BodyExecutionResult:
        """Execute a bounded action chunk and return durable acknowledgements."""

    @abstractmethod
    async def interrupt(
        self,
        reason: str,
        *,
        safe_action: ActionChunk | None = None,
    ) -> BodyExecutionResult:
        """Interrupt the current chunk and enter the configured safe state."""

    @abstractmethod
    async def health(self) -> EnvironmentHealth:
        """Return a cheap readiness/degradation signal."""

    @abstractmethod
    async def close(self) -> None:
        """Release driver resources and stop background safety tasks."""


__all__ = ["BaseBodyService"]
