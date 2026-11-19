"""Registration API for resident RoboTenon tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")
Factory = Callable[..., T]


@dataclass(frozen=True, slots=True)
class RegisteredTool(Generic[T]):
    name: str
    factory: Factory[T]
    version: str = "1.0.0"
    kind: str = "generic"
    description: str | None = None


class ToolRegistry(Generic[T]):
    """Deterministic registry addressed by one tool string."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool[T]] = {}

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
            raise ValueError("tool name cannot be empty")

        def add(candidate: Factory[T]) -> Factory[T]:
            existing = self._tools.get(name)
            if existing is not None and not replace:
                if existing.factory is candidate and existing.version == version:
                    return candidate
                raise ValueError(f"tool already registered: {name}")
            self._tools[name] = RegisteredTool(
                name=name,
                factory=candidate,
                version=version,
                kind=kind,
                description=description,
            )
            return candidate

        return add(factory) if factory is not None else add

    def get(self, name: str) -> RegisteredTool[T]:
        try:
            return self._tools[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "<none>"
            raise KeyError(f"unknown tool {name!r}; available: {available}") from exc

    def build(self, name: str, params: Mapping[str, Any] | None = None, **runtime: Any) -> T:
        values = dict(params or {})
        overlap = values.keys() & runtime.keys()
        if overlap:
            raise ValueError("tool config/runtime parameter collision: " + ", ".join(sorted(overlap)))
        return self.get(name).factory(**values, **runtime)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict[str, str | None]]:
        return [
            {
                "name": item.name,
                "version": item.version,
                "kind": item.kind,
                "description": item.description,
            }
            for item in sorted(self._tools.values(), key=lambda value: value.name)
        ]


tool_registry: ToolRegistry[Any] = ToolRegistry()


def register_tool(name: str, factory: Factory[Any] | None = None, **kwargs: Any):
    """Register a tool class/factory under a stable string."""

    return tool_registry.register(name, factory, **kwargs)


def load_builtin_tools() -> None:
    """Import built-in tool registrations once (safe to call repeatedly)."""

    from . import builtin as _tools  # noqa: F401
    from . import batch_model as _batch_model  # noqa: F401


__all__ = [
    "RegisteredTool",
    "ToolRegistry",
    "load_builtin_tools",
    "register_tool",
    "tool_registry",
]
