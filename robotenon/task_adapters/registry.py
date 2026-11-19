"""Registration and construction for Task Adapt providers."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from .base import TaskAdapter

T = TypeVar("T", bound=TaskAdapter)
Factory = Callable[..., T]


@dataclass(frozen=True, slots=True)
class RegisteredTaskAdapter(Generic[T]):
    name: str
    factory: Factory[T]
    version: str = "1.0.0"
    kind: str = "generic"
    description: str | None = None
    cli_overrides: frozenset[str] = frozenset()


class TaskAdapterRegistry(Generic[T]):
    """Deterministic registry addressed by adapter name."""

    def __init__(self) -> None:
        self._adapters: dict[str, RegisteredTaskAdapter[T]] = {}

    def register(
        self,
        name: str,
        factory: Factory[T] | None = None,
        *,
        version: str = "1.0.0",
        kind: str = "generic",
        description: str | None = None,
        replace: bool = False,
        cli_overrides: tuple[str, ...] = (),
    ):
        if not name or not name.strip():
            raise ValueError("task adapter name cannot be empty")

        def add(candidate: Factory[T]) -> Factory[T]:
            existing = self._adapters.get(name)
            if existing is not None and not replace:
                if existing.factory is candidate and existing.version == version:
                    return candidate
                raise ValueError(f"task adapter already registered: {name}")
            self._adapters[name] = RegisteredTaskAdapter(
                name=name,
                factory=candidate,
                version=version,
                kind=kind,
                description=description,
                cli_overrides=frozenset(cli_overrides),
            )
            return candidate

        return add(factory) if factory is not None else add

    def get(self, name: str) -> RegisteredTaskAdapter[T]:
        try:
            return self._adapters[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "<none>"
            raise KeyError(
                f"unknown task adapter {name!r}; available: {available}"
            ) from exc

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
                "task adapter config/runtime parameter collision: "
                + ", ".join(sorted(overlap))
            )
        return self.get(name).factory(**values, **runtime)

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def describe(self) -> list[dict[str, str | None]]:
        return [
            {
                "name": item.name,
                "version": item.version,
                "kind": item.kind,
                "description": item.description,
            }
            for item in sorted(self._adapters.values(), key=lambda value: value.name)
        ]


task_adapter_registry: TaskAdapterRegistry[Any] = TaskAdapterRegistry()


def register_task_adapter(
    name: str,
    factory: Factory[Any] | None = None,
    **kwargs: Any,
):
    return task_adapter_registry.register(name, factory, **kwargs)


def load_plugin_task_adapters(adapters: list[str] | tuple[str, ...]) -> None:
    for name in adapters:
        if not name or not name.strip():
            raise ValueError("plugin_task_adapters cannot contain an empty module name")
        importlib.import_module(name)


def load_builtin_task_adapters() -> None:
    """Load dependency-light built-ins; heavy environment imports stay lazy."""

    from . import single as _single  # noqa: F401


__all__ = [
    "RegisteredTaskAdapter",
    "TaskAdapterRegistry",
    "load_builtin_task_adapters",
    "load_plugin_task_adapters",
    "register_task_adapter",
    "task_adapter_registry",
]
