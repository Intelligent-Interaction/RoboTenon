"""Optional model providers for shared, dynamically batched inference."""

import importlib


def load_factory(reference):
    """Resolve an explicitly configured module:factory."""
    module, name = reference.split(":", 1)
    return getattr(importlib.import_module(module), name)
