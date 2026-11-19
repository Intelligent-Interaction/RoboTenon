"""Durable idempotency ledger for resident Body action execution."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..schemas import ActionExecution


class SQLiteActionExecutionStore:
    """SQLite WAL-backed action acknowledgement ledger.

    The key is ``(session_id, idempotency_key)``.  A successful acknowledgement
    survives Body process restarts, while failed/interrupted attempts are not
    recorded as completed and therefore remain retryable by the caller.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._initialize()

    @contextmanager
    def _connect(self):
        if self._closed:
            raise RuntimeError("action execution store is closed")
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS action_executions ("
                "session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, "
                "payload TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(session_id, idempotency_key))"
            )

    @staticmethod
    def _decode(payload: str) -> ActionExecution | None:
        try:
            value = json.loads(payload)
            validator = getattr(ActionExecution, "model_validate", None)
            execution = (
                validator(value)
                if callable(validator)
                else ActionExecution.parse_obj(value)
            )
            return execution if execution.status == "succeeded" else None
        except Exception:
            return None

    def load(self, session_id: Any, idempotency_key: str) -> ActionExecution | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM action_executions WHERE session_id=? AND idempotency_key=?",
                (str(session_id), idempotency_key),
            ).fetchone()
        return None if row is None else self._decode(row["payload"])

    def save(self, session_id: Any, execution: ActionExecution) -> None:
        if execution.status != "succeeded":
            return
        dump = getattr(execution, "model_dump", None)
        value = dump(mode="json") if callable(dump) else execution.dict()
        payload = json.dumps(value, ensure_ascii=False, default=str)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO action_executions(session_id,idempotency_key,payload,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(session_id,idempotency_key) DO UPDATE SET "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (
                    str(session_id),
                    execution.idempotency_key,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    async def load_async(self, session_id: Any, idempotency_key: str) -> ActionExecution | None:
        """Read from the ledger without occupying the caller's event loop."""

        return await asyncio.to_thread(self.load, session_id, idempotency_key)

    async def save_async(self, session_id: Any, execution: ActionExecution) -> None:
        """Persist an acknowledgement in a worker thread."""

        await asyncio.to_thread(self.save, session_id, execution)

    def close(self) -> None:
        with self._lock:
            self._closed = True
