"""Observation decoration for an externally owned EnvironmentService.

Environment execution lives in the resident envs service (normally exposed
through ``EnvironmentServiceServer``).  This module only contains the small
observation decorator needed to attach the URI cache at that service
boundary; it does not provide a second in-process environment implementation.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from ..schemas import EnvironmentCommand, EnvironmentFeedback, EnvironmentHealth


def invoke_observe(method: Any, request: Any = None) -> Any:
    """Call an observe method with an optional request when it accepts one."""

    if request is None:
        return method()
    try:
        parameters = inspect.signature(method).parameters.values()
        accepts = any(
            item.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            )
            for item in parameters
        )
    except (TypeError, ValueError):
        accepts = True
    return method(request) if accepts else method()


class ObservationEnvironmentService:
    """Decorate an external EnvironmentService with the observation pipeline.

    The wrapped service remains the owner of the environment session.  The
    decorator serialises reset/observe/command calls by default, returns the
    last URI-only feedback while an action is in flight when
    ``observe_policy="latest"``, and sends every feedback through the shared
    ``ObservationPipeline``.
    """

    def __init__(
        self,
        service: Any,
        observation_pipeline: Any,
        *,
        observe_policy: str = "latest",
    ):
        if service is observation_pipeline:
            raise ValueError("service and observation_pipeline must be different objects")
        if observe_policy not in {"wait", "latest", "concurrent"}:
            raise ValueError(
                "observe_policy must be 'wait', 'latest' or 'concurrent'"
            )
        self.service = service
        self.observation_pipeline = observation_pipeline
        self.observe_policy = observe_policy
        self._latest: EnvironmentFeedback | None = None
        self._session_id: Any = None
        self._lock = asyncio.Lock()
        self._action_in_flight = 0

    @property
    def latest_feedback(self) -> EnvironmentFeedback | None:
        return self._latest or getattr(self.service, "latest_feedback", None)

    @property
    def session_id(self) -> Any:
        latest = self.latest_feedback
        if latest is not None:
            return (latest.metadata or {}).get("_cache_session_id", latest.session_id)
        return getattr(self.service, "session_id", None)

    async def start(self) -> None:
        method = getattr(self.service, "start", None)
        if not callable(method):
            return
        result = method()
        if inspect.isawaitable(result):
            await result

    async def reset(self, context: Any) -> EnvironmentFeedback:
        await self.start()
        async with self._lock:
            self._action_in_flight += 1
            try:
                result = await self._call("reset", context)
                return await self._ingest(result, reset=True)
            finally:
                self._action_in_flight = max(0, self._action_in_flight - 1)

    async def observe(self, request: Any = None) -> EnvironmentFeedback:
        if (
            self.observe_policy == "latest"
            and self._action_in_flight
            and self._latest is not None
        ):
            return self._latest
        await self.start()
        if self.observe_policy == "concurrent":
            return await self._observe_unlocked(request)
        async with self._lock:
            return await self._observe_unlocked(request)

    async def command(self, command: EnvironmentCommand) -> EnvironmentFeedback:
        await self.start()
        async with self._lock:
            self._action_in_flight += 1
            try:
                result = await self._call("command", command)
                return await self._ingest(
                    result, reset=getattr(command, "type", None) == "reset"
                )
            finally:
                self._action_in_flight = max(0, self._action_in_flight - 1)

    async def health(self) -> EnvironmentHealth:
        if self._action_in_flight:
            return EnvironmentHealth(
                ready=True,
                degraded=True,
                message="action_in_flight",
            )
        return await self._call("health")

    async def close(self) -> None:
        async with self._lock:
            try:
                await self._call("close")
            finally:
                cache = getattr(self.observation_pipeline, "cache", None)
                flush = getattr(cache, "flush", None)
                if callable(flush):
                    result = flush()
                    if inspect.isawaitable(result):
                        await result

    async def _call(self, method_name: str, *args: Any) -> Any:
        method = getattr(self.service, method_name, None)
        if not callable(method):
            raise AttributeError(
                f"wrapped environment service does not implement {method_name}()"
            )
        result = method(*args)
        return await result if inspect.isawaitable(result) else result

    async def _observe_unlocked(self, request: Any = None) -> EnvironmentFeedback:
        method = getattr(self.service, "observe", None)
        if not callable(method):
            raise AttributeError(
                "wrapped environment service does not implement observe()"
            )
        result = invoke_observe(method, request)
        if inspect.isawaitable(result):
            result = await result
        return await self._ingest(result, reset=False)

    async def _ingest(self, value: Any, *, reset: bool) -> EnvironmentFeedback:
        result = await self.observation_pipeline.ingest(
            value,
            raw_payload=value,
            session_id=self._session_id,
            reset=reset,
        )
        if not isinstance(result, EnvironmentFeedback):
            raise TypeError("observation pipeline must return EnvironmentFeedback")
        self._latest = result
        self._session_id = (result.metadata or {}).get(
            "_cache_session_id", result.session_id
        )
        return result


__all__ = ["ObservationEnvironmentService", "invoke_observe"]
