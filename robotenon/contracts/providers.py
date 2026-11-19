"""Small infrastructure provider registry, independent of implementations."""
from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Provider:
    factory: Callable[..., Any]
    capabilities: frozenset[str] = frozenset()


class ProviderRegistry:
    def __init__(self, family: str, required: tuple[str, ...] = ()):
        self.family = family
        self.required = required
        self._providers: dict[str, Provider] = {}

    def register(self, name, factory=None, *, capabilities=()):
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError(f"invalid {self.family} provider name")

        def add(candidate):
            if not callable(candidate):
                raise TypeError(f"{self.family} factory must be callable")
            entry = Provider(candidate, frozenset(capabilities))
            if name in self._providers and self._providers[name] != entry:
                raise ValueError(f"{self.family} provider already registered: {name}")
            self._providers[name] = entry
            return candidate
        return add(factory) if factory is not None else add

    def load(self, modules):
        if not isinstance(modules, (list, tuple)) or any(
            not isinstance(name, str) or not name.strip() for name in modules
        ):
            raise ValueError(f"{self.family} plugins must be a list of module names")
        for name in modules:
            importlib.import_module(name)

    def get(self, name):
        try:
            return self._providers[name]
        except KeyError:
            raise ValueError(f"unknown {self.family} provider: {name!r}") from None

    def build(self, name, *, params=None, **runtime):
        if params is not None and not isinstance(params, Mapping):
            raise ValueError(f"{self.family} params must be an object")
        values = dict(params or {})
        if values.keys() & runtime.keys():
            raise ValueError(f"{self.family} config/runtime parameter collision")
        result = self.get(name).factory(**values, **runtime)
        missing = [method for method in self.required if not callable(getattr(result, method, None))]
        if missing:
            raise TypeError(f"{self.family} provider {name!r} missing methods: {', '.join(missing)}")
        return result
