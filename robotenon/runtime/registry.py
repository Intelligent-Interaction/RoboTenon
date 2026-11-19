"""One registry for every resident RoboTenon Module."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

# Compatibility re-exports keep the familiar ``runtime.registry`` import
# path available while the Tool and Module registries remain independent.
from ..tools.registry import (
    RegisteredTool,
    ToolRegistry,
    register_tool,
    tool_registry,
)

T = TypeVar("T")
Factory = Callable[..., T]


@dataclass(frozen=True, slots=True)
class RegisteredModule(Generic[T]):
    name: str
    factory: Factory[T]
    version: str = "1.0.0"
    kind: str = "generic"
    description: str | None = None


class ModuleRegistry(Generic[T]):
    """Deterministic registry addressed by one string."""

    def __init__(self) -> None:
        self._modules: dict[str, RegisteredModule[T]] = {}

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
            raise ValueError("module name cannot be empty")

        def add(candidate: Factory[T]) -> Factory[T]:
            existing = self._modules.get(name)
            if existing is not None and not replace:
                if existing.factory is candidate and existing.version == version:
                    return candidate
                raise ValueError(f"module already registered: {name}")
            self._modules[name] = RegisteredModule(
                name=name,
                factory=candidate,
                version=version,
                kind=kind,
                description=description,
            )
            return candidate

        return add(factory) if factory is not None else add

    def get(self, name: str) -> RegisteredModule[T]:
        try:
            return self._modules[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "<none>"
            raise KeyError(f"unknown module {name!r}; available: {available}") from exc

    def build(self, name: str, params: Mapping[str, Any] | None = None, **runtime: Any) -> T:
        values = dict(params or {})
        overlap = values.keys() & runtime.keys()
        if overlap:
            raise ValueError("module config/runtime parameter collision: " + ", ".join(sorted(overlap)))
        return self.get(name).factory(**values, **runtime)

    def names(self) -> list[str]:
        return sorted(self._modules)

    def describe(self) -> list[dict[str, str | None]]:
        return [
            {"name": item.name, "version": item.version, "kind": item.kind, "description": item.description}
            for item in sorted(self._modules.values(), key=lambda value: value.name)
        ]


module_registry: ModuleRegistry[Any] = ModuleRegistry()


def register_module(name: str, factory: Factory[Any] | None = None, **kwargs: Any):
    return module_registry.register(name, factory, **kwargs)


def load_builtin_components() -> None:
    from ..modules import builtin as _modules  # noqa: F401


__all__ = [
    "ModuleRegistry",
    "RegisteredModule",
    "RegisteredTool",
    "ToolRegistry",
    "load_builtin_components",
    "module_registry",
    "register_module",
    "register_tool",
    "tool_registry",
]
