from __future__ import annotations

import os
import threading
from typing import Any, Protocol

class ContextConflictError(RuntimeError):
    """Raised when a process tries to overwrite a newer global context."""


class ContextStore(Protocol):
    """Durable authority for a run's cross-process GraphContext snapshot."""

    def load(self, session_id: Any) -> dict[str, Any] | None: ...

    def save(self, context: Any, *, expected_revision: int | None = None) -> int: ...


def __getattr__(name):
    if name in {"SQLiteContextStore", "SharedContextStore"}:
        from ..storage.context import SQLiteContextStore
        return SQLiteContextStore
    raise AttributeError(name)


_default_stores: dict[str, ContextStore] = {}
_default_store_lock = threading.RLock()


def get_default_context_store() -> ContextStore | None:
    """Resolve environment configuration at assembly, never while importing."""
    path = os.environ.get("ROBOTENON_CONTEXT_DB")
    if not path:
        return None
    with _default_store_lock:
        if path not in _default_stores:
            from ..storage.context import SQLiteContextStore
            _default_stores[path] = SQLiteContextStore(path)
        return _default_stores[path]


class _DefaultContextStore:
    def load(self, session_id):
        store = get_default_context_store()
        return store.load(session_id) if store is not None else None

    def save(self, context, *, expected_revision=None):
        store = get_default_context_store()
        if store is None:
            raise RuntimeError("ROBOTENON_CONTEXT_DB is not configured; inject a ContextStore")
        return store.save(context, expected_revision=expected_revision)


global_context_store = _DefaultContextStore()
