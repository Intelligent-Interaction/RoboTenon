"""Registration API for resident Body service implementations.

Body is deliberately a separate extension namespace from Modules and Tools.
A registered body factory creates the process-resident service that owns the
low-level action driver (or an IPC client for a remote body process).  The
registry stores factories only; lifecycle and singleton ownership belong to
the bootstrap/runtime that builds the selected service.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")
Factory = Callable[..., T]


@dataclass(frozen=True, slots=True)
class RegisteredBody(Generic[T]):
    """Metadata and factory for one Body provider."""

    name: str
    factory: Factory[T]
    version: str = "1.0.0"
    kind: str = "generic"
    description: str | None = None


class BodyRegistry(Generic[T]):
    """Deterministic registry addressed by one body string.

    Constructor values from ``body_configs.json`` and runtime dependencies
    (for example ``executor`` or ``safety_state``) are kept separate.  A
    collision is rejected instead of silently letting one source override the
    other, which makes deployment mistakes visible at startup.
    """

    def __init__(self) -> None:
        self._bodies: dict[str, RegisteredBody[T]] = {}

    def register(
        self,
        name: str,
        factory: Factory[T] | None = None,
        *,
        version: str = "1.0.0",
        kind: str = "generic",
        description: str | None = None,
        replace: bool = False,
    ):
        if not name or not name.strip():
            raise ValueError("body name cannot be empty")

        def add(candidate: Factory[T]) -> Factory[T]:
            existing = self._bodies.get(name)
            if existing is not None and not replace:
                if existing.factory is candidate and existing.version == version:
                    return candidate
                raise ValueError(f"body already registered: {name}")
            self._bodies[name] = RegisteredBody(
                name=name,
                factory=candidate,
                version=version,
                kind=kind,
                description=description,
            )
            return candidate

        return add(factory) if factory is not None else add

    def get(self, name: str) -> RegisteredBody[T]:
        try:
            return self._bodies[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "<none>"
            raise KeyError(f"unknown body {name!r}; available: {available}") from exc

    def build(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        **runtime: Any,
    ) -> T:
        values = dict(params or {})
        overlap = values.keys() & runtime.keys()
        if overlap:
            raise ValueError(
                "body config/runtime parameter collision: "
                + ", ".join(sorted(overlap))
            )
        return self.get(name).factory(**values, **runtime)

    def names(self) -> list[str]:
        return sorted(self._bodies)

    def describe(self) -> list[dict[str, str | None]]:
        return [
            {
                "name": item.name,
                "version": item.version,
                "kind": item.kind,
                "description": item.description,
            }
            for item in sorted(self._bodies.values(), key=lambda value: value.name)
        ]


body_registry: BodyRegistry[Any] = BodyRegistry()


def register_body(name: str, factory: Factory[Any] | None = None, **kwargs: Any):
    """Register a Body factory under a stable string."""

    return body_registry.register(name, factory, **kwargs)


def load_builtin_bodies() -> None:
    """Import built-in Body registrations (safe to call repeatedly)."""

    from . import builtin as _builtin  # noqa: F401
    from . import real_robot_body as _real_robot_body  # noqa: F401


__all__ = [
    "BodyRegistry",
    "RegisteredBody",
    "body_registry",
    "load_builtin_bodies",
    "register_body",
]
