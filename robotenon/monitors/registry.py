"""Registration API for resident/request monitor plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")
Factory = Callable[..., T]


@dataclass(frozen=True, slots=True)
class RegisteredMonitor(Generic[T]):
    name: str
    factory: Factory[T]
    version: str = "1.0.0"
    kind: str = "generic"
    description: str | None = None


class MonitorRegistry(Generic[T]):
    """Deterministic registry addressed by one monitor string."""

    def __init__(self) -> None:
        self._monitors: dict[str, RegisteredMonitor[T]] = {}

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
            raise ValueError("monitor name cannot be empty")

        def add(candidate: Factory[T]) -> Factory[T]:
            existing = self._monitors.get(name)
            if existing is not None and not replace:
                if existing.factory is candidate and existing.version == version:
                    return candidate
                raise ValueError(f"monitor already registered: {name}")
            self._monitors[name] = RegisteredMonitor(
                name=name,
                factory=candidate,
                version=version,
                kind=kind,
                description=description,
            )
            return candidate

        return add(factory) if factory is not None else add

    def get(self, name: str) -> RegisteredMonitor[T]:
        try:
            return self._monitors[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "<none>"
            raise KeyError(f"unknown monitor {name!r}; available: {available}") from exc

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
                "monitor config/runtime parameter collision: "
                + ", ".join(sorted(overlap))
            )
        return self.get(name).factory(**values, **runtime)

    def names(self) -> list[str]:
        return sorted(self._monitors)

    def describe(self) -> list[dict[str, str | None]]:
        return [
            {
                "name": item.name,
                "version": item.version,
                "kind": item.kind,
                "description": item.description,
            }
            for item in sorted(self._monitors.values(), key=lambda value: value.name)
        ]


monitor_registry: MonitorRegistry[Any] = MonitorRegistry()


def register_monitor(
    name: str,
    factory: Factory[Any] | None = None,
    **kwargs: Any,
):
    """Register a monitor class/factory under a stable string."""

    return monitor_registry.register(name, factory, **kwargs)


def load_builtin_monitors() -> None:
    """Import built-in monitor registrations (safe to call repeatedly)."""

    from . import builtin as _builtin  # noqa: F401


__all__ = [
    "MonitorRegistry",
    "RegisteredMonitor",
    "load_builtin_monitors",
    "monitor_registry",
    "register_monitor",
]
