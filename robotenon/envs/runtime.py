"""Process-resident handle for the currently active environment service.

Environment is deliberately not a graph-module dependency.  Workflow and
monitor processes talk to an EnvironmentService, while graph modules receive
only the shared ``ctx``.  The service is registered here for the lifetime of
the process and can be used from any module with, for example::

    from robotenon.envs import environment

    feedback = await environment.observe()

The handle is a small singleton proxy rather than a second environment
connection.  Binding an environment is normally done by the process
bootstrap (or by ``GraphRuntime`` for an in-process session).  ``bind``
returns the previous value so a caller can restore it
when a run ends.  Applications should bind one environment at a time; a
multi-robot deployment should use one environment service process per
endpoint and expose that endpoint to modules through its own IPC client.
"""

from __future__ import annotations

import inspect
import threading
from typing import Any


class EnvironmentNotBoundError(RuntimeError):
    """Raised when a module tries to use the singleton before bootstrap."""


class EnvironmentHandle:
    """Singleton proxy delegating environment calls to the bound service.

    The explicit methods keep the public contract type-safe and work with
    both an ``EnvironmentSession`` and a remote ``EnvironmentService``.  A
    service may implement synchronous methods in a small test adapter; those
    values are accepted and returned by the async proxy unchanged after
    awaiting only when necessary.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._service: Any = None

    def bind(self, service: Any) -> Any:
        """Bind ``service`` and return the previously bound service."""

        if service is None:
            raise ValueError("cannot bind a null environment service")
        if service is self:
            raise ValueError("cannot bind the environment handle to itself")
        # Binding is idempotent even when the current value is the
        # ObservationEnvironmentService decorator around the same underlying
        # service.  Without this check every GraphRuntime run that receives a
        # bare service would add another wrapper and ingest each feedback
        # repeatedly through nested pipelines.
        with self._lock:
            current = self._service
            if current is service or getattr(current, "service", None) is service:
                return current
        # Keep the ergonomic ``environment.bind(session)`` spelling
        # transparent as well as the explicit ``bind_environment`` helper.
        # The lazy lookup avoids importing infrastructure while the package
        # is still being discovered, and services that already carry a
        # pipeline are never wrapped twice.
        if getattr(service, "observation_pipeline", None) is None:
            try:
                from ..infra.observation_runtime import get_observation_pipeline

                pipeline = get_observation_pipeline()
                if pipeline is not None:
                    from ..services.environment import ObservationEnvironmentService

                    service = ObservationEnvironmentService(service, pipeline)
            except Exception:
                # Binding an environment must remain usable when optional
                # observation infrastructure is unavailable.
                pass
        with self._lock:
            previous = self._service
            self._service = service
            return previous

    set = bind

    def restore(self, previous: Any) -> None:
        """Restore a value returned by :meth:`bind` (``None`` unbinds)."""

        with self._lock:
            self._service = previous

    def unbind(self, expected: Any = None) -> Any:
        """Clear the current service and return it.

        If ``expected`` is supplied, the handle is cleared only when the
        current value is that exact object.  This avoids a completed run
        accidentally unbinding an environment installed by a newer owner.
        """

        with self._lock:
            current = self._service
            if expected is None or current is expected:
                self._service = None
            return current

    @property
    def current(self) -> Any:
        """Return the bound service, or ``None`` when not configured."""

        with self._lock:
            return self._service

    @property
    def service(self) -> Any:
        """Alias for :attr:`current` used by service-oriented integrations."""

        return self.current

    def get(self, *, required: bool = False) -> Any:
        """Return the current service, optionally requiring bootstrap."""

        return self.require() if required else self.current

    def require(self) -> Any:
        """Return the service or raise a descriptive setup error."""

        service = self.current
        if service is None:
            raise EnvironmentNotBoundError(
                "no environment service is bound; call environment.bind(service) "
                "during application bootstrap"
            )
        return service

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        target = getattr(self.require(), method, None)
        if not callable(target):
            raise AttributeError(
                f"bound environment does not implement {method}()"
            )
        value = target(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    async def start(self) -> Any:
        return await self._call("start")

    async def reset(self, context: Any) -> Any:
        return await self._call("reset", context)

    async def observe(self, request: Any = None) -> Any:
        return await self._call("observe", request) if request is not None else await self._call("observe")

    async def command(self, command: Any) -> Any:
        return await self._call("command", command)

    async def health(self) -> Any:
        return await self._call("health")

    async def close(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("close", *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Expose adapter-specific read-only helpers without copying state."""

        # Only resolve dynamic attributes after normal class attributes.  The
        # returned callable remains bound to the service and is useful for
        # vendor-specific diagnostics; standard calls should use the methods
        # above so sync/async behavior is normalized.
        service = self.require()
        return getattr(service, name)


# One process-wide handle.  It is intentionally named ``environment`` so a
# graph module can use ``await environment.observe()`` without receiving env
# as a positional/keyword input.
environment = EnvironmentHandle()
environment_singleton = environment
env = environment
EnvironmentSingleton = EnvironmentHandle


def get_environment() -> EnvironmentHandle:
    """Return the process-wide environment handle."""

    return environment


def bind_environment(service: Any, *, observation_pipeline: Any = None) -> Any:
    """Bind a service on the process-wide handle and return its predecessor.

    When the application bootstrap configured a process-resident observation
    pipeline, a bare service is decorated here automatically.  Callers may
    override that pipeline explicitly; services already carrying one are not
    wrapped twice.
    """

    pipeline = observation_pipeline
    if pipeline is None:
        try:
            from ..infra.observation_runtime import get_observation_pipeline

            pipeline = get_observation_pipeline()
        except Exception:
            pipeline = None
    current = environment.current
    if current is service or getattr(current, "service", None) is service:
        # Preserve the existing wrapper and make restoration a no-op.  This
        # is important when a Workflow is handed the underlying remote/local
        # service while bootstrap has already installed its observation
        # decorator.
        return current
    if pipeline is not None and getattr(service, "observation_pipeline", None) is None:
        from ..services.environment import ObservationEnvironmentService

        service = ObservationEnvironmentService(service, pipeline)
    return environment.bind(service)


def unbind_environment(expected: Any = None) -> Any:
    """Unbind the process-wide service; see :meth:`EnvironmentHandle.unbind`."""

    return environment.unbind(expected)


__all__ = [
    "EnvironmentHandle",
    "EnvironmentNotBoundError",
    "EnvironmentSingleton",
    "bind_environment",
    "env",
    "environment",
    "environment_singleton",
    "get_environment",
    "unbind_environment",
]
