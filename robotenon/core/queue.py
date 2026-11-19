"""Optional persistent task-item queue for explicitly integrated controllers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from ..errors import LeaseLostError
from ..schemas import TaskItem, TaskStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TaskQueue:
    """SQLite-backed queue with leases, retries, branching and an event log.

    The default Graph/episode/parallel runtimes do not consume this queue.
    An integrating controller claims and completes/fails items as their owner.
    Every transition
    is recorded in ``queue_events`` for replay and Portal inspection.
    """

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        # SQLite itself is synchronous.  Public async methods dispatch their
        # short transactions to a worker thread so a busy WAL lock never
        # stalls the Workflow event loop.  The connection remains serialized
        # by this re-entrant lock because it is shared by those worker calls.
        self._db_lock = threading.RLock()
        self._closed = False
        self._prepare_schema()
        if self.path != ":memory:":
            from ..infra.run_index import register_queue_if_indexed
            register_queue_if_indexed(self.path)

    def _prepare_schema(self) -> None:
        """Create the canonical schema and migrate one early table name once."""
        self._db.execute("PRAGMA journal_mode=WAL")
        tables = {
            row["name"]
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # Local demo databases made by the first scaffold are migrated in
        # place; the public API and new writes use only the canonical names.
        if "task_items" not in tables and "subtasks" in tables:
            self._db.execute("ALTER TABLE subtasks RENAME TO task_items")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_items (
              task_item_id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              task_id TEXT NOT NULL,
              goal TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL,
              timeout_s REAL NOT NULL DEFAULT 30,
              max_retries INTEGER NOT NULL DEFAULT 2,
              on_failure TEXT NOT NULL DEFAULT 'retry',
              lease_owner TEXT,
              lease_expires_at TEXT,
              parent_id TEXT,
              metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_items_claim
              ON task_items(session_id, status, ordinal, created_at);
            CREATE TABLE IF NOT EXISTS queue_events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_item_id TEXT,
              event_type TEXT NOT NULL,
              emitted_at TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            """
        )
        columns = {
            row["name"]
            for row in self._db.execute("PRAGMA table_info(task_items)").fetchall()
        }
        if "task_item_id" not in columns and "subtask_id" in columns:
            self._db.execute(
                "ALTER TABLE task_items RENAME COLUMN subtask_id TO task_item_id"
            )
        columns = {
            row["name"]
            for row in self._db.execute("PRAGMA table_info(task_items)").fetchall()
        }
        for name, declaration in (
            ("timeout_s", "REAL NOT NULL DEFAULT 30"),
            ("max_retries", "INTEGER NOT NULL DEFAULT 2"),
            ("on_failure", "TEXT NOT NULL DEFAULT 'retry'"),
        ):
            if name not in columns:
                self._db.execute(f"ALTER TABLE task_items ADD COLUMN {name} {declaration}")
        event_columns = {
            row["name"]
            for row in self._db.execute("PRAGMA table_info(queue_events)").fetchall()
        }
        if "task_item_id" not in event_columns and "subtask_id" in event_columns:
            self._db.execute(
                "ALTER TABLE queue_events RENAME COLUMN subtask_id TO task_item_id"
            )
        self._db.commit()

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> TaskItem:
        return TaskItem(
            task_item_id=UUID(row["task_item_id"]),
            session_id=UUID(row["session_id"]),
            task_id=row["task_id"],
            goal=row["goal"],
            ordinal=row["ordinal"],
            status=row["status"],
            attempts=row["attempts"],
            timeout_s=row["timeout_s"],
            max_retries=row["max_retries"],
            on_failure=row["on_failure"],
            lease_owner=row["lease_owner"],
            lease_expires_at=(
                datetime.fromisoformat(row["lease_expires_at"])
                if row["lease_expires_at"]
                else None
            ),
            parent_id=UUID(row["parent_id"]) if row["parent_id"] else None,
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def _event(
        self,
        task_item_id: UUID | None,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO queue_events(task_item_id,event_type,emitted_at,payload_json) VALUES(?,?,?,?)",
            (
                str(task_item_id) if task_item_id else None,
                event_type,
                _now().isoformat(),
                json.dumps(payload or {}, default=str),
            ),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("queue is closed")

    async def _run_db(self, operation, *args):
        """Run one serialized SQLite operation off the asyncio loop."""

        async with self._lock:
            self._ensure_open()
            return await asyncio.to_thread(self._run_db_sync, operation, *args)

    def _run_db_sync(self, operation, *args):
        with self._db_lock:
            try:
                return operation(*args)
            except BaseException:
                # Several public operations use an explicit transaction and
                # may fail after a write (for example a duplicate task id or
                # a lease conflict). Roll the shared connection back before
                # the next worker-thread operation; otherwise sqlite keeps
                # the failed transaction open and later calls can observe
                # half-applied state.
                try:
                    self._db.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _insert_task_sync(self, task: TaskItem) -> None:
        now = _now().isoformat()
        self._db.execute(
            """INSERT INTO task_items(task_item_id,session_id,task_id,goal,ordinal,status,attempts,
               timeout_s,max_retries,on_failure,lease_owner,lease_expires_at,parent_id,metadata_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(task.task_item_id),
                str(task.session_id),
                task.task_id,
                task.goal,
                task.ordinal,
                task.status.value,
                task.attempts,
                task.timeout_s,
                task.max_retries,
                task.on_failure,
                task.lease_owner,
                task.lease_expires_at.isoformat() if task.lease_expires_at else None,
                str(task.parent_id) if task.parent_id else None,
                json.dumps(task.metadata, default=str),
                now,
                now,
            ),
        )
        self._event(
            task.task_item_id,
            "enqueued",
            {"goal": task.goal, "ordinal": task.ordinal},
        )

    async def enqueue(self, task: TaskItem) -> TaskItem:
        return await self._run_db(self._enqueue_sync, task)

    def _enqueue_sync(self, task: TaskItem) -> TaskItem:
        self._insert_task_sync(task)
        self._db.commit()
        return task

    async def enqueue_plan(self, *, session_id: UUID, task_id: str, goals: list[str]) -> list[TaskItem]:
        tasks = [
            TaskItem(session_id=session_id, task_id=task_id, goal=goal, ordinal=ordinal)
            for ordinal, goal in enumerate(goals)
        ]
        return await self.enqueue_tasks(tasks)

    async def enqueue_tasks(self, tasks: list[TaskItem]) -> list[TaskItem]:
        for task in tasks:
            await self.enqueue(task)
        return tasks

    def _recover_expired_sync(self, session_id: UUID | None = None) -> int:
        now = _now().isoformat()
        where = "session_id = ? AND " if session_id else ""
        args: tuple[Any, ...] = (str(session_id), now) if session_id else (now,)
        rows = self._db.execute(
            f"SELECT task_item_id FROM task_items WHERE {where} status = 'running' "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            args,
        ).fetchall()
        for row in rows:
            task_item_id = UUID(row["task_item_id"])
            self._db.execute(
                "UPDATE task_items SET status='pending', lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE task_item_id=?",
                (now, str(task_item_id)),
            )
            self._event(task_item_id, "lease_expired")
        return len(rows)

    async def claim_next(
        self, *, session_id: UUID, owner: str, lease_seconds: float = 30.0
    ) -> TaskItem | None:
        if not str(owner).strip():
            raise ValueError("queue lease owner must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        return await self._run_db(
            self._claim_next_sync, session_id, owner, lease_seconds
        )

    def _claim_next_sync(
        self, session_id: UUID, owner: str, lease_seconds: float
    ) -> TaskItem | None:
        # The claim is a read-modify-write operation. BEGIN IMMEDIATE makes
        # the pending-row selection and conditional update one transaction so
        # two Workflow processes sharing a queue cannot both claim the same
        # task when their local asyncio locks are independent.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._recover_expired_sync(session_id)
            row = self._db.execute(
                "SELECT * FROM task_items WHERE session_id=? AND status='pending' "
                "ORDER BY ordinal,created_at LIMIT 1",
                (str(session_id),),
            ).fetchone()
            if row is None:
                self._db.commit()
                return None
            task_item_id = UUID(row["task_item_id"])
            now = _now()
            expires = now + timedelta(seconds=lease_seconds)
            updated_count = self._db.execute(
                "UPDATE task_items SET status='running', attempts=attempts+1, lease_owner=?, "
                "lease_expires_at=?, updated_at=? WHERE task_item_id=? AND status='pending'",
                (owner, expires.isoformat(), now.isoformat(), str(task_item_id)),
            ).rowcount
            if updated_count != 1:
                # Defensive guard for a legacy trigger or an external writer
                # that changed the row between SELECT and UPDATE. The
                # transaction is rolled back and the caller sees no false
                # claim.
                self._db.rollback()
                return None
            self._event(
                task_item_id,
                "claimed",
                {"owner": owner, "lease_expires_at": expires.isoformat()},
            )
            self._db.commit()
            updated = self._db.execute(
                "SELECT * FROM task_items WHERE task_item_id=?", (str(task_item_id),)
            ).fetchone()
            return self._row_to_model(updated)
        except BaseException:
            self._db.rollback()
            raise

    async def next_task(
        self, *, session_id: UUID, owner: str, lease_seconds: float = 30.0
    ) -> TaskItem | None:
        """Claim the next pending item for a Workflow."""
        return await self.claim_next(
            session_id=session_id, owner=owner, lease_seconds=lease_seconds
        )

    async def complete(self, task_item_id: UUID, *, owner: str | None = None) -> TaskItem:
        return await self._transition(task_item_id, TaskStatus.DONE, owner=owner, event_type="completed")

    async def fail(
        self,
        task_item_id: UUID,
        *,
        owner: str | None = None,
        retry: bool = False,
        reason: str | None = None,
    ) -> TaskItem:
        # Retry eligibility depends on the current attempts counter. Read the
        # row, choose the status and clear the lease in one transaction so two
        # independent Workflow processes cannot both turn the same failure
        # into an extra retry.
        return await self._run_db(
            self._fail_sync, task_item_id, owner, retry, reason
        )

    def _fail_sync(
        self,
        task_item_id: UUID,
        owner: str | None,
        retry: bool,
        reason: str | None,
    ) -> TaskItem:
        # Retry eligibility depends on the attempts value.  Lock the read and
        # update in one transaction so two queue processes cannot both turn a
        # single failure into another retry or race an owner check.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT * FROM task_items WHERE task_item_id=?", (str(task_item_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task item: {task_item_id}")
            current_status = TaskStatus(row["status"])
            status = TaskStatus.FAILED
            if retry and row["attempts"] <= row["max_retries"]:
                status = TaskStatus.PENDING
            # A duplicate failure notification can arrive after the first
            # process has already released the lease.  Treat an identical
            # terminal/retry state as idempotent instead of emitting a second
            # queue event.  A different state remains a programming error.
            if current_status is status and row["lease_owner"] is None:
                if owner is None or self._last_transition_owner_sync(task_item_id) == owner:
                    self._db.commit()
                    return self._row_to_model(row)
            # Once a caller supplies an owner, an active item must still be
            # leased to that exact owner.  Allowing ``NULL`` here lets a stale
            # workflow complete an unclaimed pending item after another
            # process has released its lease.  The idempotent return above is
            # intentionally allowed for duplicate notifications.
            if owner and current_status in {TaskStatus.RUNNING, TaskStatus.PENDING} and row["lease_owner"] != owner:
                raise LeaseLostError(
                    "task item lease belongs to another owner",
                    details={"task_item_id": str(task_item_id)},
                )
            if current_status not in {TaskStatus.RUNNING, TaskStatus.PENDING}:
                raise RuntimeError(
                    f"cannot fail task item {task_item_id} from status {current_status.value}"
                )
            now = _now().isoformat()
            updated_count = self._db.execute(
                "UPDATE task_items SET status=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE task_item_id=?",
                (status.value, now, str(task_item_id)),
            ).rowcount
            if updated_count != 1:
                raise RuntimeError(f"task item disappeared during failure: {task_item_id}")
            self._event(
                task_item_id,
                "retry" if status is TaskStatus.PENDING else "failed",
                {
                    **({"reason": reason} if reason else {}),
                    **({"owner": owner} if owner else {}),
                },
            )
            self._db.commit()
            updated = self._db.execute(
                "SELECT * FROM task_items WHERE task_item_id=?", (str(task_item_id),)
            ).fetchone()
            return self._row_to_model(updated)
        except BaseException:
            self._db.rollback()
            raise

    def _retry_status_sync(self, task_item_id: UUID) -> TaskStatus:
        row = self._db.execute(
            "SELECT attempts,max_retries FROM task_items WHERE task_item_id=?",
            (str(task_item_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown task item: {task_item_id}")
        return (
            TaskStatus.PENDING
            if row["attempts"] <= row["max_retries"]
            else TaskStatus.FAILED
        )

    async def cancel(self, task_item_id: UUID, *, reason: str | None = None) -> TaskItem:
        return await self._transition(
            task_item_id, TaskStatus.CANCELLED, owner=None, event_type="cancelled", reason=reason
        )

    async def clear(self, *, session_id: UUID | None = None, reason: str = "cleared") -> int:
        """Cancel all pending/running items, optionally limited to a session."""
        return await self._run_db(self._clear_sync, session_id, reason)

    def _clear_sync(self, session_id: UUID | None, reason: str) -> int:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            where = "status IN ('pending','running')"
            args: tuple[Any, ...] = ()
            if session_id is not None:
                where = "session_id=? AND " + where
                args = (str(session_id),)
            rows = self._db.execute(
                f"SELECT task_item_id FROM task_items WHERE {where}", args
            ).fetchall()
            now = _now().isoformat()
            for row in rows:
                task_item_id = UUID(row["task_item_id"])
                self._db.execute(
                    "UPDATE task_items SET status='cancelled', lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE task_item_id=?",
                    (now, str(task_item_id)),
                )
                self._event(task_item_id, "cleared", {"reason": reason})
            self._event(
                None,
                "queue_cleared",
                {
                    "session_id": str(session_id) if session_id else None,
                    "count": len(rows),
                    "reason": reason,
                },
            )
            self._db.commit()
            return len(rows)
        except BaseException:
            self._db.rollback()
            raise

    async def branch(self, *, parent_id: UUID, tasks: list[TaskItem]) -> list[TaskItem]:
        """Insert evaluator-requested branch items under an existing item."""
        return await self._run_db(self._branch_sync, parent_id, tasks)

    def _branch_sync(self, parent_id: UUID, tasks: list[TaskItem]) -> list[TaskItem]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            parent = self._db.execute(
                "SELECT task_item_id FROM task_items WHERE task_item_id=?", (str(parent_id),)
            ).fetchone()
            if parent is None:
                raise KeyError(f"unknown parent task item: {parent_id}")
            for task in tasks:
                task.parent_id = parent_id
                self._insert_task_sync(task)
            self._event(
                parent_id,
                "branched",
                {"children": [str(task.task_item_id) for task in tasks]},
            )
            self._db.commit()
            return tasks
        except BaseException:
            self._db.rollback()
            raise

    async def reset(self, *, session_id: UUID, reason: str) -> int:
        """Cancel unfinished work and emit a replayable reset event."""
        return await self.clear(session_id=session_id, reason=reason)

    async def _transition(
        self,
        task_item_id: UUID,
        status: TaskStatus,
        *,
        owner: str | None,
        event_type: str,
        reason: str | None = None,
    ) -> TaskItem:
        return await self._run_db(
            self._transition_sync, task_item_id, status, owner, event_type, reason
        )

    def _transition_sync(
        self,
        task_item_id: UUID,
        status: TaskStatus,
        owner: str | None,
        event_type: str,
        reason: str | None,
    ) -> TaskItem:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT * FROM task_items WHERE task_item_id=?", (str(task_item_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task item: {task_item_id}")
            current_status = TaskStatus(row["status"])
            if current_status is status and row["lease_owner"] is None:
                if owner is None or self._last_transition_owner_sync(task_item_id) == owner:
                    self._db.commit()
                    return self._row_to_model(row)
            if owner and current_status in {TaskStatus.RUNNING, TaskStatus.PENDING} and row["lease_owner"] != owner:
                raise LeaseLostError(
                    "task item lease belongs to another owner",
                    details={"task_item_id": str(task_item_id)},
                )
            if current_status in {
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                raise RuntimeError(
                    f"cannot transition task item {task_item_id} from status {current_status.value}"
                )
            now = _now().isoformat()
            updated_count = self._db.execute(
                "UPDATE task_items SET status=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE task_item_id=?",
                (status.value, now, str(task_item_id)),
            ).rowcount
            if updated_count != 1:
                raise RuntimeError(f"task item disappeared during transition: {task_item_id}")
            self._event(
                task_item_id,
                event_type,
                {
                    **({"reason": reason} if reason else {}),
                    **({"owner": owner} if owner else {}),
                },
            )
            self._db.commit()
            return self._row_to_model(
                self._db.execute(
                    "SELECT * FROM task_items WHERE task_item_id=?", (str(task_item_id),)
                ).fetchone()
            )
        except BaseException:
            self._db.rollback()
            raise

    async def snapshot(self, *, session_id: UUID | None = None) -> list[TaskItem]:
        return await self._run_db(self._snapshot_sync, session_id)

    def _snapshot_sync(self, session_id: UUID | None) -> list[TaskItem]:
        if session_id:
            rows = self._db.execute(
                "SELECT * FROM task_items WHERE session_id=? ORDER BY ordinal,created_at",
                (str(session_id),),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM task_items ORDER BY created_at"
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def _last_transition_owner_sync(self, task_item_id: UUID) -> str | None:
        """Return the owner that produced the last state transition.

        ``lease_owner`` is cleared as soon as a task is completed or retried.
        Recording the owner in the event payload lets us distinguish a
        duplicate acknowledgement from a stale process attempting to mutate
        an unclaimed item, without adding another mutable column to the queue
        schema.  Old databases simply return ``None`` and therefore require
        the caller to omit ``owner`` for an idempotent legacy transition.
        """

        row = self._db.execute(
            "SELECT payload_json FROM queue_events "
            "WHERE task_item_id=? AND event_type IN "
            "('completed','retry','failed','cancelled','cleared') "
            "ORDER BY event_id DESC LIMIT 1",
            (str(task_item_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            return None
        value = payload.get("owner") if isinstance(payload, dict) else None
        return str(value) if value is not None else None

    async def events(self, *, task_item_id: UUID | None = None) -> list[dict[str, Any]]:
        return await self._run_db(self._events_sync, task_item_id)

    def _events_sync(self, task_item_id: UUID | None) -> list[dict[str, Any]]:
        if task_item_id:
            rows = self._db.execute(
                "SELECT * FROM queue_events WHERE task_item_id=? ORDER BY event_id",
                (str(task_item_id),),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM queue_events ORDER BY event_id"
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "task_item_id": row["task_item_id"],
                "event_type": row["event_type"],
                "emitted_at": row["emitted_at"],
                "payload": json.loads(row["payload_json"] or "{}"),
            }
            for row in rows
        ]

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                self._closed = True
                await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._db_lock:
            self._db.commit()
            self._db.close()


__all__ = ["TaskQueue"]
