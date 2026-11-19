"""CAS-protected SQLite checkpoints; no dependency on the graph engine."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.context import ContextConflictError
from ..contracts.serialization import jsonable


class SQLiteContextStore:
    """SQLite WAL-backed global context store.

    ``GraphContext`` remains a mutable object inside Workflow.  When Env,
    Body and Monitor run in different processes they cannot share Python
    identity, so this store is the single serialized authority.  Every write
    uses compare-and-swap on ``context_revision``; a stale service cannot
    silently overwrite a newer graph state.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self):
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
                "CREATE TABLE IF NOT EXISTS graph_contexts ("
                "session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
                "payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    @staticmethod
    def _session_key(session_id: Any) -> str:
        return str(session_id)

    @staticmethod
    def _dump(context: Any, *, revision: int | None = None) -> str:
        dump = getattr(context, "to_dict", None)
        value = dump() if callable(dump) else context
        if revision is not None and isinstance(value, dict):
            # Keep the serialized payload and the row-level CAS revision in
            # sync.  ``load`` still trusts the row in case an older database
            # contains a stale payload field.
            value = dict(value)
            value["context_revision"] = revision
        # Checkpoint state is a wire contract.  Silently converting an
        # arbitrary object to ``str`` makes a restart appear successful while
        # irreversibly changing application state.  Use the shared strict
        # codec and reject NaN/Infinity so corruption is reported at the
        # persistence boundary.
        return json.dumps(
            jsonable(value), ensure_ascii=False, allow_nan=False
        )

    def load(self, session_id: Any) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload, revision FROM graph_contexts WHERE session_id = ?",
                (self._session_key(session_id),),
            ).fetchone()
            if row is None:
                return None
            value = json.loads(row["payload"])
            if isinstance(value, dict):
                value["context_revision"] = int(row["revision"])
            return value

    def save(self, context: Any, *, expected_revision: int | None = None) -> int:
        session_id = getattr(context, "session_id", None)
        if session_id is None:
            raise ValueError(
                "context.session_id is required before persisting a global context"
            )
        key = self._session_key(session_id)
        expected = expected_revision
        if expected is None:
            expected = int(getattr(context, "context_revision", 0) or 0)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM graph_contexts WHERE session_id = ?", (key,)
            ).fetchone()
            current = int(row["revision"]) if row is not None else 0
            if row is not None and current != expected:
                raise ContextConflictError(
                    f"context revision conflict for {key}: expected {expected}, found {current}"
                )
            revision = current + 1
            payload = self._dump(context, revision=revision)
            connection.execute(
                "INSERT INTO graph_contexts(session_id, revision, payload, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision, "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (key, revision, payload, now),
            )
        if hasattr(context, "context_revision"):
            context.context_revision = revision
        return revision

    def delete(self, session_id: Any) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM graph_contexts WHERE session_id = ?",
                (self._session_key(session_id),),
            )
