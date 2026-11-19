from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ...schemas import (
    EnvironmentCommand,
    EnvironmentDescriptor,
    EnvironmentFeedback,
    EnvironmentHealth,
    SessionSpec,
)


class EnvironmentSession(Protocol):
    """Per-run channel bound behind the process-wide environment handle.

    Bootstrap binds the session with ``bind_environment``.  GraphRuntime
    passes its shared ``GraphContext`` to ``reset``.  Adapters may also accept
    a ``SessionSpec`` when they create a session from a declarative
    specification; both forms stay outside the Module registry.
    """

    session_id: Any

    async def reset(self, context: Any) -> EnvironmentFeedback:
        """Reset this session from a GraphContext or SessionSpec."""

    async def observe(self, request: Any = None) -> EnvironmentFeedback:
        """Return the newest feedback without applying an Action.

        ``request`` is optional for adapters that support a camera/profile
        selection; simple adapters may ignore it.
        """

    async def close(self, reason: str | None = None) -> None:
        """Release hardware or process resources."""

    async def health(self) -> EnvironmentHealth:
        """Return a cheap readiness signal."""

    async def command(self, command: EnvironmentCommand) -> EnvironmentFeedback:
        """Execute a declared control/diagnostic command."""


class EnvironmentAdapter(Protocol):
    """Factory and descriptor for an env plugin.

    The runtime intentionally knows nothing about simulator or robot SDKs.  A
    plugin translates its native API to this protocol and keeps that dependency
    outside the core package.
    """

    descriptor: EnvironmentDescriptor

    def describe(self) -> EnvironmentDescriptor: ...

    async def connect(self) -> None: ...

    async def open(self, spec: SessionSpec) -> EnvironmentSession:
        ...

    async def create_session(self, spec: SessionSpec) -> EnvironmentSession: ...

    async def close(self) -> None: ...

    async def health(self) -> EnvironmentHealth:
        ...


EnvironmentAdapterFactory = Callable[..., EnvironmentAdapter]


class EnvironmentRegistry:
    """Registry of resident EnvironmentAdapter factories.

    Adapters are not Modules and never enter ``ModuleRuntime``.  A registered
    factory is instantiated lazily once and its adapter is cached; each run
    asks that resident adapter to create a short-lived EnvironmentSession.
    """

    def __init__(self) -> None:
        self._factories: dict[str, EnvironmentAdapterFactory] = {}
        self._adapters: dict[str, EnvironmentAdapter] = {}
        self._adapter_params: dict[str, dict[str, Any]] = {}

    def register(
        self,
        env_id: str | EnvironmentAdapter,
        factory: EnvironmentAdapterFactory | None = None,
        *,
        replace: bool = False,
    ) -> EnvironmentAdapterFactory:
        """Register a factory, or wrap an already-created adapter instance.

        The instance form remains useful for tests and is still cached as a
        resident adapter.  New integrations should use the factory/decorator
        form so constructor parameters stay outside the Module config.
        """
        if not isinstance(env_id, str):
            adapter = env_id
            env_id = adapter.descriptor.env_id
            if factory is not None:
                raise TypeError("factory cannot be supplied when registering an adapter instance")
            factory = lambda adapter=adapter: adapter
        if not env_id or not env_id.strip():
            raise ValueError("environment id cannot be empty")
        if factory is None:
            def add(candidate: EnvironmentAdapterFactory) -> EnvironmentAdapterFactory:
                return self.register(env_id, candidate, replace=replace)

            return add
        if env_id in self._factories and not replace:
            raise ValueError(f"environment already registered: {env_id}")
        self._factories[env_id] = factory
        if replace:
            self._adapters.pop(env_id, None)
            self._adapter_params.pop(env_id, None)
        return factory

    def register_factory(
        self,
        env_id: str,
        factory: EnvironmentAdapterFactory | None = None,
        *,
        replace: bool = False,
    ) -> EnvironmentAdapterFactory:
        return self.register(env_id, factory, replace=replace)

    def get(self, env_id: str, **params: Any) -> EnvironmentAdapter:
        """Return the resident adapter, constructing it at most once."""
        if env_id not in self._factories:
            raise KeyError(f"unknown environment: {env_id}")
        if env_id not in self._adapters:
            adapter = self._factories[env_id](**params)
            actual_id = adapter.descriptor.env_id
            if actual_id != env_id:
                raise ValueError(
                    f"environment factory {env_id!r} returned descriptor {actual_id!r}"
                )
            self._adapters[env_id] = adapter
            self._adapter_params[env_id] = dict(params)
        elif params and params != self._adapter_params.get(env_id, {}):
            raise ValueError(
                f"environment adapter {env_id!r} is already resident; "
                "constructor params cannot change"
            )
        return self._adapters[env_id]

    def build(
        self,
        env_id: str,
        params: Mapping[str, Any] | None = None,
        **runtime: Any,
    ) -> EnvironmentAdapter:
        """Resolve an adapter from separated config/runtime values.

        Unlike Module/Tool/Body runtimes, an EnvironmentAdapter is resident
        and cached by this registry.  Calling ``build`` therefore returns the
        same adapter as ``get`` and rejects duplicate constructor keys rather
        than silently changing an active endpoint.
        """

        values = dict(params or {})
        overlap = values.keys() & runtime.keys()
        if overlap:
            raise ValueError(
                "environment config/runtime parameter collision: "
                + ", ".join(sorted(overlap))
            )
        return self.get(env_id, **values, **runtime)

    def create_session(self, env_id: str, *args: Any, **params: Any) -> EnvironmentSession:
        """Create one session from the resident adapter.

        The returned value may be awaitable when an adapter exposes an async
        ``open``/``create_session`` method; callers can await it as needed.
        """
        adapter = self.get(env_id, **params.pop("adapter_params", {}))
        opener = getattr(adapter, "open", None) or getattr(adapter, "create_session", None)
        if not callable(opener):
            raise TypeError(f"environment adapter {env_id!r} does not implement open/create_session")
        return opener(*args, **params)

    def list(self) -> list[EnvironmentDescriptor]:
        return [adapter.descriptor for adapter in self._adapters.values()]

    def names(self) -> list[str]:
        return sorted(self._factories)

    def clear(self) -> None:
        """Forget registrations; callers own closing already-created adapters."""
        self._factories.clear()
        self._adapters.clear()
        self._adapter_params.clear()


environment_registry = EnvironmentRegistry()


def register_environment(
    env_id: str,
    factory: EnvironmentAdapterFactory | None = None,
    *,
    replace: bool = False,
) -> EnvironmentAdapterFactory:
    """Decorator/function form for the independent environment registry."""
    return environment_registry.register(env_id, factory, replace=replace)


def load_builtin_environments() -> None:
    """Register dependency-free development adapters on explicit bootstrap."""

    # Importing ``robotenon.envs`` should not mutate the global registry.  The
    # fake adapter is registered only when an application actually asks for
    # built-ins (normally from ``build_environment_adapter``).
    if "fake" not in environment_registry.names():
        from .fake import FakeEnvironmentAdapter

        environment_registry.register("fake", FakeEnvironmentAdapter)


__all__ = [
    "EnvironmentAdapter",
    "EnvironmentAdapterFactory",
    "EnvironmentRegistry",
    "EnvironmentSession",
    "environment_registry",
    "load_builtin_environments",
    "register_environment",
]
