"""Durable idempotency ledger for resident Body action execution."""

from __future__ import annotations

from typing import Any, Protocol

from ..schemas import ActionExecution


class ActionExecutionStore(Protocol):
    """Persistence boundary used by Body to deduplicate retried actions."""

    def load(self, session_id: Any, idempotency_key: str) -> ActionExecution | None: ...

    def save(self, session_id: Any, execution: ActionExecution) -> None: ...

    # Async adapters are optional.  Body falls back to ``asyncio.to_thread``
    # for legacy synchronous stores, so a slow SQLite/NFS ledger can never
    # block the control event loop.
    async def load_async(self, session_id: Any, idempotency_key: str) -> ActionExecution | None: ...

    async def save_async(self, session_id: Any, execution: ActionExecution) -> None: ...


def __getattr__(name):
    if name == "SQLiteActionExecutionStore":
        from ..storage.actions import SQLiteActionExecutionStore
        return SQLiteActionExecutionStore
    raise AttributeError(name)


__all__ = ["ActionExecutionStore", "SQLiteActionExecutionStore"]  # noqa: F822 - lazy compatibility exports
