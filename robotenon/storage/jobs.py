"""Indexed experiment registry with a bounded working set and legacy import."""

import base64
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import MutableMapping
from contextlib import closing
from pathlib import Path

from ..infra.history import page_window


class JobStore(MutableMapping):
    def __init__(self, root, *, cache_size=128, cache_bytes=8 * 1024 * 1024):
        self.path = Path(root) / "jobs.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_size, self.cache_bytes = cache_size, cache_bytes
        self._cache = OrderedDict()
        self._original = {}
        self._sizes = {}
        self._lock = threading.RLock()
        with self._connect() as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, "
                "submitted_at TEXT NOT NULL, status TEXT NOT NULL, active INTEGER NOT NULL, "
                "search_text TEXT NOT NULL, record_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS jobs_time ON jobs(submitted_at DESC, job_id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS jobs_active ON jobs(active, submitted_at, job_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, submitted_at, job_id)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS registry_meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            if not conn.execute(
                "SELECT 1 FROM registry_meta WHERE key='legacy_imported'"
            ).fetchone():
                legacy = self.path.with_name("jobs.json")
                if legacy.exists():
                    values = json.loads(legacy.read_text(encoding="utf-8"))
                    values = values.values() if isinstance(values, dict) else values
                    if not isinstance(values, (list, type({}.values()))):
                        raise ValueError(
                            "legacy jobs.json must contain an experiment list or object"
                        )
                    for value in values:
                        if isinstance(value, dict) and value.get("job_id"):
                            self._save(conn, value)
                conn.execute("INSERT INTO registry_meta VALUES ('legacy_imported','1')")

    def _connect(self):
        return closing(sqlite3.connect(self.path, timeout=1))

    @staticmethod
    def _encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @classmethod
    def _save(cls, conn, value, encoded=None):
        status = str(value.get("status", ""))
        progress = value.get("progress") or {}
        if not isinstance(progress, dict):
            progress = {}
        active = (
            status in {"running", "stopping", "stop_failed"}
            or progress.get("status") == "running"
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "submitted_at=excluded.submitted_at, status=excluded.status, active=excluded.active, "
            "search_text=excluded.search_text, record_json=excluded.record_json",
            (
                str(value["job_id"]),
                value.get("submitted_at", ""),
                status,
                int(active),
                " ".join(
                    str(value.get(k, ""))
                    for k in ("job_id", "config", "status", "submitted_at")
                ).lower(),
                encoded if encoded is not None else cls._encode(value),
            ),
        )

    def _flush_keys(self, keys):
        changes = [(key, self._encode(self._cache[key])) for key in keys]
        changes = [
            (key, encoded)
            for key, encoded in changes
            if encoded != self._original.get(key)
        ]
        if changes:
            with self._connect() as conn, conn:
                for key, encoded in changes:
                    self._save(conn, self._cache[key], encoded)
            self._original.update(changes)
            self._sizes.update(
                (key, len(encoded.encode("utf-8"))) for key, encoded in changes
            )

    def flush(self):
        with self._lock:
            self._flush_keys(list(self._cache))
            self._trim()

    def _trim(self):
        # Retain the newest object even when one individual record exceeds the budget.
        while len(self._cache) > 1 and (
            len(self._cache) > self.cache_size
            or sum(self._sizes.values()) > self.cache_bytes
        ):
            key = next(iter(self._cache))
            self._flush_keys([key])
            self._cache.pop(key)
            self._original.pop(key, None)
            self._sizes.pop(key, None)

    def __getitem__(self, key):
        with self._lock:
            if key not in self._cache:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT record_json FROM jobs WHERE job_id=?", (key,)
                    ).fetchone()
                if row is None:
                    raise KeyError(key)
                self._cache[key] = json.loads(row[0])
                self._original[key] = row[0]
                self._sizes[key] = len(row[0].encode("utf-8"))
            self._cache.move_to_end(key)
            self._trim()
            return self._cache[key]

    def __setitem__(self, key, value):
        if str(value.get("job_id")) != str(key):
            raise ValueError("experiment registry key does not match job_id")
        with self._lock:
            self._cache[key] = value
            self._sizes[key] = len(self._encode(value).encode("utf-8"))
            self._cache.move_to_end(key)
            self._trim()

    def __delitem__(self, key):
        with self._lock, self._connect() as conn, conn:
            if key not in self:
                raise KeyError(key)
            conn.execute("DELETE FROM jobs WHERE job_id=?", (key,))
            self._cache.pop(key, None)
            self._original.pop(key, None)
            self._sizes.pop(key, None)

    def __contains__(self, key):
        with self._lock:
            if key in self._cache:
                return True
            with self._connect() as conn:
                return (
                    conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (key,)).fetchone()
                    is not None
                )

    def __len__(self):
        self.flush()
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT count(*) FROM jobs").fetchone()[0]

    def __iter__(self):
        self.flush()
        last = ""
        while True:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT job_id FROM jobs WHERE job_id>? ORDER BY job_id LIMIT 256",
                    (last,),
                ).fetchall()
            if not rows:
                return
            for (last,) in rows:
                yield last

    def active_keys(self, *, waiting=True):
        self.flush()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id FROM jobs WHERE active=1"
                + (" OR status='waiting'" if waiting else "")
                + " ORDER BY submitted_at, job_id"
            ).fetchall()
        return [row[0] for row in rows]

    def waiting_keys(self):
        self.flush()
        stamp, key = "", ""
        while True:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT submitted_at,job_id FROM jobs WHERE status='waiting' "
                    "AND (submitted_at,job_id)>(?,?) ORDER BY submitted_at,job_id LIMIT 128",
                    (stamp, key),
                ).fetchall()
            if not rows:
                return
            for stamp, key in rows:
                yield key

    def page(
        self, *, limit=100, offset=0, since=None, until=None, search="", cursor=None
    ):
        limit, offset, since, until = page_window(limit, offset, since, until)
        if cursor and offset:
            raise ValueError("use cursor or offset, not both")
        filters, args = [], []
        for value, clause in (
            (since, "submitted_at>=?"),
            (until, "submitted_at<=?"),
            (str(search).lower() if search else None, "instr(search_text,?)>0"),
        ):
            if value is not None:
                filters.append(clause)
                args.append(value)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        self.flush()
        deadline = time.monotonic() + 1
        with self._lock, self._connect() as conn:
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                overview = dict(
                    conn.execute(
                        "SELECT status,count(*) FROM jobs" + where + " GROUP BY status",
                        args,
                    )
                )
                if cursor:
                    try:
                        stamp, key, scope = json.loads(base64.urlsafe_b64decode(cursor))
                        if scope != [since, until, search]:
                            raise ValueError("cursor belongs to a different query")
                    except Exception as exc:
                        raise ValueError("invalid experiment cursor") from exc
                    filters.append("(submitted_at,job_id)<(?,?)")
                    args.extend((stamp, key))
                where = " WHERE " + " AND ".join(filters) if filters else ""
                rows = conn.execute(
                    "SELECT job_id,submitted_at FROM jobs"
                    + where
                    + " ORDER BY submitted_at DESC,job_id DESC LIMIT ? OFFSET ?",
                    (*args, limit + 1, offset),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc):
                    raise TimeoutError(
                        "experiment query exceeded its time budget; narrow the query"
                    ) from exc
                raise
        more, rows = len(rows) > limit, rows[:limit]
        token = (
            base64.urlsafe_b64encode(
                json.dumps([rows[-1][1], rows[-1][0], [since, until, search]]).encode()
            ).decode()
            if more
            else None
        )
        return {
            "job_ids": [row[0] for row in rows],
            "overview": overview,
            "total": sum(overview.values()),
            "limit": limit,
            "offset": offset,
            "next_offset": offset + len(rows) if more else None,
            "next_cursor": token,
        }
