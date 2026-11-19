"""Transactional SQLite implementation of the safety store contract."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from ..runtime.safety import SafetyStateStore, merge_signal, safety_mode
from ..schemas import SafetyLevel, SafetySignal, SafetyStateSnapshot


class SQLiteSafetyStateStore(SafetyStateStore):
    """Cross-process safety state backed by one SQLite WAL file.

    ``SafetyStateStore`` is sufficient when the four runtime components live
    in one process.  When Environment, Monitor, Workflow and Body are started
    independently, construct this class with the same path in each process.
    Updates are transactionally visible to all readers and P3/P4 signals stay
    latched until an explicit clear, just like the in-memory implementation.
    """

    # The synchronous methods remain part of the watchdog-friendly public
    # contract, but an async service must dispatch them to a worker thread so
    # SQLite busy timeouts and WAL commits cannot stall its event loop.
    non_blocking = False

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                "CREATE TABLE IF NOT EXISTS safety_signals ("
                "code TEXT PRIMARY KEY, payload TEXT NOT NULL, latched INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS safety_meta ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL, "
                "updated_at TEXT NOT NULL, incident_id TEXT, incident_started_at TEXT)"
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(safety_meta)"
                ).fetchall()
            }
            if "incident_id" not in columns:
                connection.execute(
                    "ALTER TABLE safety_meta ADD COLUMN incident_id TEXT"
                )
            if "incident_started_at" not in columns:
                connection.execute(
                    "ALTER TABLE safety_meta ADD COLUMN incident_started_at TEXT"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS safety_heartbeats ("
                "source TEXT PRIMARY KEY, expires_at TEXT NOT NULL, level TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO safety_meta(id, revision, updated_at) VALUES (1, 0, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )

    @staticmethod
    def _dump_signal(signal: SafetySignal) -> str:
        dump = getattr(signal, "model_dump", None)
        value = dump(mode="json") if callable(dump) else signal.dict()
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _load_signal(payload: str) -> SafetySignal:
        value = json.loads(payload)
        validator = getattr(SafetySignal, "model_validate", None)
        return (
            validator(value) if callable(validator) else SafetySignal.parse_obj(value)
        )

    @staticmethod
    def _snapshot_from_connection(
        connection: sqlite3.Connection,
    ) -> SafetyStateSnapshot:
        rows = connection.execute(
            "SELECT payload FROM safety_signals ORDER BY code"
        ).fetchall()
        signals = [SQLiteSafetyStateStore._load_signal(row["payload"]) for row in rows]
        now = datetime.now(timezone.utc)
        heartbeat_rows = connection.execute(
            "SELECT source, expires_at, level FROM safety_heartbeats"
        ).fetchall()
        for row in heartbeat_rows:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at <= now:
                try:
                    level = SafetyLevel(str(row["level"]).upper())
                except ValueError:
                    level = SafetyLevel.P3
                signals.append(
                    SafetySignal(
                        code=f"{row['source']}.heartbeat",
                        level=level,
                        source=row["source"],
                        message="service heartbeat expired",
                        timestamp=now,
                        expires_at=expires_at,
                        latched=False,
                    )
                )
        level = max(
            (signal.level for signal in signals),
            key=lambda item: item.rank,
            default=SafetyLevel.P0,
        )
        meta = connection.execute(
            "SELECT revision, updated_at, incident_id, incident_started_at FROM safety_meta WHERE id = 1"
        ).fetchone()
        # Keep the incident identity durable and consistent for every process.
        # The conditional UPDATE is important: two independent readers may
        # observe a new fault at the same time, but they must converge on one
        # incident id rather than dispatching two recovery graphs.
        if level.rank > SafetyLevel.P0.rank and (
            meta is None or not meta["incident_id"]
        ):
            candidate = uuid4()
            started = now.isoformat()
            connection.execute(
                "UPDATE safety_meta SET incident_id=?, incident_started_at=?, "
                "revision=revision+1, updated_at=? WHERE id=1 AND incident_id IS NULL",
                (str(candidate), started, started),
            )
            meta = connection.execute(
                "SELECT revision, updated_at, incident_id, incident_started_at FROM safety_meta WHERE id = 1"
            ).fetchone()
        elif level is SafetyLevel.P0 and meta is not None and meta["incident_id"]:
            cleared_at = now.isoformat()
            connection.execute(
                "UPDATE safety_meta SET incident_id=NULL, incident_started_at=NULL, "
                "revision=revision+1, updated_at=? WHERE id=1 AND incident_id IS NOT NULL",
                (cleared_at,),
            )
            meta = connection.execute(
                "SELECT revision, updated_at, incident_id, incident_started_at FROM safety_meta WHERE id = 1"
            ).fetchone()
        updated_at = (
            datetime.fromisoformat(meta["updated_at"])
            if meta
            else datetime.now(timezone.utc)
        )
        incident_id = None
        incident_started_at = None
        if meta and meta["incident_id"]:
            try:
                incident_id = UUID(meta["incident_id"])
            except (ValueError, TypeError):
                incident_id = None
        if meta and meta["incident_started_at"]:
            try:
                incident_started_at = datetime.fromisoformat(
                    meta["incident_started_at"]
                )
            except (ValueError, TypeError):
                incident_started_at = None
        return SafetyStateSnapshot(
            level=level,
            mode=safety_mode(level),
            signals=signals,
            revision=int(meta["revision"]) if meta else 0,
            updated_at=updated_at,
            incident_id=incident_id,
            incident_started_at=incident_started_at,
        )

    def snapshot(self) -> SafetyStateSnapshot:
        with self._lock, self._connect() as connection:
            return self._snapshot_from_connection(connection)

    def update(
        self,
        signals: Iterable[SafetySignal] = (),
        *,
        level: SafetyLevel | str | None = None,
        source: str = "safety",
        replace_transient: bool = True,
    ) -> SafetyStateSnapshot:
        incoming = list(signals)
        if level is not None:
            normalized = (
                level
                if isinstance(level, SafetyLevel)
                else SafetyLevel(str(level).upper())
            )
            if not any(signal.level == normalized for signal in incoming):
                incoming.append(
                    SafetySignal(
                        code=f"{source}.level",
                        level=normalized,
                        source=source,
                        latched=normalized.rank >= SafetyLevel.P3.rank,
                    )
                )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if replace_transient:
                connection.execute("DELETE FROM safety_signals WHERE latched = 0")
            for signal in incoming:
                row = connection.execute(
                    "SELECT payload FROM safety_signals WHERE code = ?", (signal.code,)
                ).fetchone()
                signal = merge_signal(
                    self._load_signal(row["payload"]) if row else None, signal
                )
                connection.execute(
                    "INSERT INTO safety_signals(code, payload, latched) VALUES (?, ?, ?) "
                    "ON CONFLICT(code) DO UPDATE SET payload=excluded.payload, latched=excluded.latched",
                    (signal.code, self._dump_signal(signal), int(signal.latched)),
                )
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "UPDATE safety_meta SET revision = revision + 1, updated_at = ? WHERE id = 1",
                (now,),
            )
            return self._snapshot_from_connection(connection)

    def reconcile(
        self,
        owner: str,
        signals: Iterable[SafetySignal] = (),
        *,
        replace_transient: bool = True,
        clear_latched: bool = False,
    ) -> SafetyStateSnapshot:
        """Atomically replace the current non-latched signals for one owner."""

        owner = str(owner).strip()
        if not owner:
            raise ValueError("safety signal owner must be non-empty")
        incoming: list[SafetySignal] = []
        for signal in signals:
            metadata = dict(signal.metadata or {})
            metadata.setdefault("_owner", owner)
            incoming.append(signal.model_copy(update={"metadata": metadata}))
        incoming_codes = {signal.code for signal in incoming}

        def owned(code: str, signal: SafetySignal) -> bool:
            metadata = signal.metadata or {}
            return (
                metadata.get("_owner") == owner
                or signal.source == owner
                or code == f"{owner}.compliance"
                or code.startswith(f"{owner}.")
            )

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if replace_transient:
                rows = connection.execute(
                    "SELECT code, payload FROM safety_signals"
                ).fetchall()
                for row in rows:
                    previous = self._load_signal(row["payload"])
                    if (
                        owned(row["code"], previous)
                        and row["code"] not in incoming_codes
                        and (clear_latched or not previous.latched)
                    ):
                        connection.execute(
                            "DELETE FROM safety_signals WHERE code = ?",
                            (row["code"],),
                        )
            for signal in incoming:
                row = connection.execute(
                    "SELECT payload FROM safety_signals WHERE code = ?",
                    (signal.code,),
                ).fetchone()
                merged = merge_signal(
                    self._load_signal(row["payload"]) if row else None,
                    signal,
                )
                connection.execute(
                    "INSERT INTO safety_signals(code, payload, latched) VALUES (?, ?, ?) "
                    "ON CONFLICT(code) DO UPDATE SET payload=excluded.payload, latched=excluded.latched",
                    (merged.code, self._dump_signal(merged), int(merged.latched)),
                )
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "UPDATE safety_meta SET revision = revision + 1, updated_at = ? WHERE id = 1",
                (now,),
            )
            return self._snapshot_from_connection(connection)

    def clear(
        self, *, code: str | None = None, include_latched: bool = False
    ) -> SafetyStateSnapshot:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if code is not None:
                if include_latched:
                    deleted = connection.execute(
                        "DELETE FROM safety_signals WHERE code = ?", (code,)
                    ).rowcount
                else:
                    deleted = connection.execute(
                        "DELETE FROM safety_signals WHERE code = ? AND latched = 0",
                        (code,),
                    ).rowcount
            elif include_latched:
                deleted = connection.execute("DELETE FROM safety_signals").rowcount
            else:
                deleted = connection.execute(
                    "DELETE FROM safety_signals WHERE latched = 0"
                ).rowcount
            if deleted:
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "UPDATE safety_meta SET revision = revision + 1, updated_at = ? WHERE id = 1",
                    (now,),
                )
            return self._snapshot_from_connection(connection)

    def heartbeat(
        self,
        source: str,
        *,
        ttl_s: float = 3.0,
        failure_level: SafetyLevel | str = SafetyLevel.P3,
    ) -> SafetyStateSnapshot:
        if not source or ttl_s <= 0:
            raise ValueError(
                "heartbeat source must be non-empty and ttl_s must be positive"
            )
        level = (
            failure_level
            if isinstance(failure_level, SafetyLevel)
            else SafetyLevel(str(failure_level).upper())
        )
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO safety_heartbeats(source, expires_at, level, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET expires_at=excluded.expires_at, "
                "level=excluded.level, updated_at=excluded.updated_at",
                (
                    source,
                    (now + timedelta(seconds=ttl_s)).isoformat(),
                    level.value,
                    now.isoformat(),
                ),
            )
            return self._snapshot_from_connection(connection)

    def clear_heartbeat(self, source: str) -> SafetyStateSnapshot:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM safety_heartbeats WHERE source = ?", (source,)
            ).rowcount
            if deleted:
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "UPDATE safety_meta SET revision=revision+1, updated_at=? WHERE id=1",
                    (now,),
                )
            return self._snapshot_from_connection(connection)

    def recover(
        self,
        level: SafetyLevel | str | None = None,
        *,
        approved: bool = False,
        include_latched: bool | None = None,
    ) -> SafetyStateSnapshot:
        current = self.snapshot().level
        target = (
            current
            if level is None
            else (
                level
                if isinstance(level, SafetyLevel)
                else SafetyLevel(str(level).upper())
            )
        )
        if target.rank >= SafetyLevel.P3.rank and not approved:
            raise PermissionError(
                f"manual acknowledgement required to recover {target.value}"
            )
        clear_latched = (
            target.rank >= SafetyLevel.P3.rank
            if include_latched is None
            else include_latched
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT code, payload, latched FROM safety_signals"
            ).fetchall()
            for row in rows:
                signal = self._load_signal(row["payload"])
                if signal.level.rank > target.rank or (
                    signal.latched and not clear_latched
                ):
                    continue
                connection.execute(
                    "DELETE FROM safety_signals WHERE code = ?", (row["code"],)
                )
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "UPDATE safety_meta SET revision = revision + 1, updated_at = ? WHERE id = 1",
                (now,),
            )
            return self._snapshot_from_connection(connection)
