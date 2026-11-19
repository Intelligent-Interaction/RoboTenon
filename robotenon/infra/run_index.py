"""Persistent, bounded read index for episode run manifests."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

INDEX_FILENAME = ".robotenon-run-index.sqlite3"
DIRTY_FILENAME = ".robotenon-run-index.dirty"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_manifests (
    path TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_manifests_started
    ON run_manifests(started_at, path);
CREATE TABLE IF NOT EXISTS queue_databases (
    path TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS run_index_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class RunManifestIndex:
    """Index legacy manifests once and query recent runs with a SQL limit."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / INDEX_FILENAME
        self._lock = threading.RLock()

    def list(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        limit = max(1, min(int(limit), 10_000))
        with self._lock:
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                self._backfill_once(connection)
                self._reconcile_if_dirty(connection)
                rows = self._existing_manifest_rows(connection, limit)
            finally:
                connection.close()
        records = []
        # Keep the historical API's chronological order while selecting only
        # the newest `limit` rows in SQLite.
        for relative_path, payload in reversed(rows):
            try:
                record = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            record["manifest_path"] = str(self.root / relative_path)
            records.append(record)
        return records

    def register(self, manifest_path: str | Path) -> None:
        path = Path(manifest_path).expanduser().resolve()
        try:
            relative_path = path.relative_to(self.root).as_posix()
        except ValueError:
            return
        row = self._read_manifest(path)
        with self._lock:
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                if row is None:
                    connection.execute(
                        "DELETE FROM run_manifests WHERE path=?", (relative_path,)
                    )
                else:
                    started_at, payload = row
                    connection.execute(
                        "INSERT INTO run_manifests(path, started_at, record_json) "
                        "VALUES (?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                        "started_at=excluded.started_at, record_json=excluded.record_json",
                        (relative_path, started_at, payload),
                    )
                connection.commit()
            finally:
                connection.close()

    def register_queue(self, queue_path: str | Path) -> None:
        path = Path(queue_path).expanduser().resolve()
        try:
            relative_path = path.relative_to(self.root).as_posix()
        except ValueError:
            return
        with self._lock:
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                if path.is_file():
                    connection.execute(
                        "INSERT OR IGNORE INTO queue_databases(path) VALUES (?)",
                        (relative_path,),
                    )
                else:
                    connection.execute(
                        "DELETE FROM queue_databases WHERE path=?", (relative_path,)
                    )
                connection.commit()
            finally:
                connection.close()

    def queue_paths(self, *, limit: int = 256) -> list[Path]:
        if not self.root.is_dir():
            return []
        limit = max(1, min(int(limit), 1024))
        with self._lock:
            connection = self._connect()
            try:
                self._ensure_schema(connection)
                self._backfill_once(connection)
                self._reconcile_if_dirty(connection)
                rows = connection.execute(
                    "SELECT path FROM queue_databases ORDER BY path LIMIT ?", (limit,)
                ).fetchall()
                missing = [row[0] for row in rows if not (self.root / row[0]).is_file()]
                if missing:
                    connection.executemany(
                        "DELETE FROM queue_databases WHERE path=?",
                        [(item,) for item in missing],
                    )
                    connection.commit()
                    rows = connection.execute(
                        "SELECT path FROM queue_databases ORDER BY path LIMIT ?",
                        (limit,),
                    ).fetchall()
            finally:
                connection.close()
        return [self.root / row[0] for row in rows]

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(_SCHEMA)
        connection.commit()

    def _backfill_once(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT value FROM run_index_metadata WHERE key='backfill_complete'"
        ).fetchone()
        if not row or row[0] != "1":
            statement = (
                "INSERT INTO run_manifests(path, started_at, record_json) VALUES (?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET started_at=excluded.started_at, "
                "record_json=excluded.record_json"
            )
            batch = []
            for path in self.root.glob("**/manifest.json"):
                try:
                    relative_path = path.resolve().relative_to(self.root).as_posix()
                except (OSError, ValueError):
                    continue
                record = self._read_manifest(path)
                if record is None:
                    continue
                started_at, payload = record
                batch.append((relative_path, started_at, payload))
                if len(batch) >= 250:
                    connection.executemany(statement, batch)
                    connection.commit()
                    batch.clear()
            if batch:
                connection.executemany(statement, batch)
            connection.execute(
                "INSERT INTO run_index_metadata(key, value) VALUES ('backfill_complete', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
        queue_row = connection.execute(
            "SELECT value FROM run_index_metadata WHERE key='queue_backfill_complete'"
        ).fetchone()
        if not queue_row or queue_row[0] != "1":
            queue_batch = []
            for path in self.root.glob("**/queue.sqlite3"):
                if not path.is_file():
                    continue
                try:
                    relative_path = path.resolve().relative_to(self.root).as_posix()
                except (OSError, ValueError):
                    continue
                queue_batch.append((relative_path,))
            connection.executemany(
                "INSERT OR IGNORE INTO queue_databases(path) VALUES (?)", queue_batch
            )
            connection.execute(
                "INSERT INTO run_index_metadata(key, value) "
                "VALUES ('queue_backfill_complete', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
        connection.commit()

    def _existing_manifest_rows(
        self, connection: sqlite3.Connection, limit: int
    ) -> list[tuple[str, str]]:
        while True:
            rows = connection.execute(
                "SELECT path, record_json FROM run_manifests "
                "ORDER BY started_at DESC, path DESC LIMIT ?",
                (limit,),
            ).fetchall()
            missing = [row[0] for row in rows if not (self.root / row[0]).is_file()]
            if not missing:
                return rows
            connection.executemany(
                "DELETE FROM run_manifests WHERE path=?", [(item,) for item in missing]
            )
            connection.commit()

    def _reconcile_if_dirty(self, connection: sqlite3.Connection) -> None:
        marker = self.root / DIRTY_FILENAME
        if not marker.is_file():
            return
        manifest_paths = {
            path.resolve().relative_to(self.root).as_posix()
            for path in self.root.glob("**/manifest.json")
            if path.is_file()
        }
        queue_paths = {
            path.resolve().relative_to(self.root).as_posix()
            for path in self.root.glob("**/queue.sqlite3")
            if path.is_file()
        }
        for relative_path in manifest_paths:
            row = self._read_manifest(self.root / relative_path)
            if row is not None:
                connection.execute(
                    "INSERT INTO run_manifests(path, started_at, record_json) VALUES (?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET started_at=excluded.started_at, "
                    "record_json=excluded.record_json",
                    (relative_path, row[0], row[1]),
                )
        indexed_manifests = {
            row[0] for row in connection.execute("SELECT path FROM run_manifests")
        }
        connection.executemany(
            "DELETE FROM run_manifests WHERE path=?",
            [(path,) for path in indexed_manifests - manifest_paths],
        )
        connection.executemany(
            "INSERT OR IGNORE INTO queue_databases(path) VALUES (?)",
            [(path,) for path in queue_paths],
        )
        indexed_queues = {
            row[0] for row in connection.execute("SELECT path FROM queue_databases")
        }
        connection.executemany(
            "DELETE FROM queue_databases WHERE path=?",
            [(path,) for path in indexed_queues - queue_paths],
        )
        connection.commit()
        try:
            marker.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _read_manifest(path: Path) -> tuple[str, str] | None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict):
            return None
        started_at = str(record.get("started_at") or "")
        return started_at, json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def register_if_indexed(manifest_path: str | Path) -> bool:
    """Update the nearest existing Portal run index, if one has been created."""
    path = Path(manifest_path).expanduser().resolve()
    for parent in (path.parent, *path.parent.parents):
        index_path = parent / INDEX_FILENAME
        if not index_path.is_file():
            continue
        try:
            RunManifestIndex(parent).register(path)
            return True
        except (OSError, sqlite3.Error):
            _mark_dirty(parent)
            return False
    return False


def register_queue_if_indexed(queue_path: str | Path) -> bool:
    """Register a queue database in the nearest existing Portal catalog."""

    path = Path(queue_path).expanduser().resolve()
    for parent in (path.parent, *path.parent.parents):
        if not (parent / INDEX_FILENAME).is_file():
            continue
        try:
            RunManifestIndex(parent).register_queue(path)
            return True
        except (OSError, sqlite3.Error):
            _mark_dirty(parent)
            return False
    return False


def _mark_dirty(root: Path) -> None:
    try:
        (root / DIRTY_FILENAME).touch(exist_ok=True)
    except OSError:
        pass


__all__ = [
    "DIRTY_FILENAME",
    "INDEX_FILENAME",
    "RunManifestIndex",
    "register_if_indexed",
    "register_queue_if_indexed",
]
