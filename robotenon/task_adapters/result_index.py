"""Incremental, disposable episode index; individual JSON artifacts remain authoritative."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ..infra.history import page_window
from ..infra.trace_query import TraceQuery


class IndexRecoveryPending(RuntimeError):
    pass


class ResultIndex:
    def __init__(self, root):
        self.path = Path(root) / "episodes.sqlite3"

    @staticmethod
    def schema(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS episodes (adapter_id TEXT, global_index INTEGER, "
            "updated_at TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(adapter_id, global_index))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS episodes_time ON episodes(updated_at, global_index, adapter_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS episodes_order ON episodes(global_index, adapter_id)"
        )

    def initialize(self, records, *, clean=True):
        with closing(sqlite3.connect(self.path, timeout=1)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self.schema(conn)
            conn.execute("DELETE FROM episodes")
            conn.executemany(
                "INSERT INTO episodes VALUES (?,?,?,?)",
                (self._row(item) for item in records),
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta VALUES ('clean',?)",
                (str(int(clean)),),
            )

    def needs_recovery(self):
        if not self.path.is_file():
            return True
        try:
            with closing(
                sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2
                )
            ) as conn:
                value = conn.execute(
                    "SELECT value FROM index_meta WHERE key='clean'"
                ).fetchone()
                return value != ("1",)
        except sqlite3.DatabaseError:
            return True

    def recover(self):
        """Rebuild from authoritative case artifacts while holding the run lease.

        A staging database protects the old index if any case is invalid. No
        final summary is required; interrupted runs only have compact counts.
        """
        import tempfile

        from ..runtime.retention import _terminal_lease
        from .evaluator import _compact_episode, write_json

        root = self.path.parent.resolve()
        with _terminal_lease(root) as safe:
            if not safe:
                raise RuntimeError(
                    "run is active, queued for resume, or has no verifiable run lease"
                )
            descriptor, temporary = tempfile.mkstemp(
                prefix=".episodes-recovery-", suffix=".sqlite3", dir=root
            )
            os.close(descriptor)
            temporary = Path(temporary)
            count, counts, successes, adapters = 0, {}, 0, {}
            try:
                with closing(sqlite3.connect(temporary)) as conn, conn:
                    self.schema(conn)
                    for directory, dirs, names in os.walk(root, followlinks=False):
                        dirs[:] = [
                            name
                            for name in dirs
                            if name
                            not in {
                                ".control",
                                ".processes",
                                ".live",
                                "workers",
                                "attempts",
                            }
                            and not name.endswith(".trace")
                            and not (Path(directory) / name).is_symlink()
                        ]
                        for name in names:
                            path = Path(directory) / name
                            if (
                                not name.startswith("episode_")
                                or not name.endswith(".json")
                                or name.endswith(".memory.json")
                                or path.is_symlink()
                            ):
                                continue
                            record = json.loads(path.read_text(encoding="utf-8"))
                            if (
                                not isinstance(record, dict)
                                or not isinstance(record.get("adapter_id"), str)
                                or type(record.get("global_index")) is not int
                                or record.get("status")
                                not in {
                                    "pending",
                                    "running",
                                    "completed",
                                    "error",
                                    "cancelled",
                                }
                            ):
                                raise ValueError(f"invalid episode artifact: {path}")
                            record["episode_json"] = str(path)
                            compact = _compact_episode(record)
                            conn.execute(
                                "INSERT INTO episodes VALUES (?,?,?,?)",
                                self._row(compact),
                            )
                            status = record["status"]
                            counts[status] = counts.get(status, 0) + 1
                            successes += int(
                                status == "completed"
                                and bool(record.get("task_success"))
                            )
                            stats = adapters.setdefault(
                                record["adapter_id"],
                                {
                                    "num_cases": 0,
                                    "num_episodes": 0,
                                    "num_completed": 0,
                                    "num_errors": 0,
                                    "num_cancelled": 0,
                                    "successes": 0,
                                },
                            )
                            stats["num_cases"] += 1
                            stats["num_episodes"] += int(
                                status not in {"pending", "running"}
                            )
                            stats["num_completed"] += int(status == "completed")
                            stats["num_errors"] += int(status == "error")
                            stats["num_cancelled"] += int(status == "cancelled")
                            stats["successes"] += int(
                                status == "completed"
                                and bool(record.get("task_success"))
                            )
                            count += 1
                    if not count:
                        raise ValueError(
                            "no authoritative episode artifacts found; index was left unchanged"
                        )
                    conn.execute("INSERT INTO index_meta VALUES ('clean','0')")
                status_path = root / "status.json"
                status = (
                    json.loads(status_path.read_text()) if status_path.exists() else {}
                )
                # The lease excludes the evaluator. Clear old WAL before the
                # atomic replacement so it cannot be replayed onto the new DB.
                if self.path.exists():
                    try:
                        with closing(sqlite3.connect(self.path, timeout=1)) as old:
                            if old.execute(
                                "PRAGMA wal_checkpoint(TRUNCATE)"
                            ).fetchone()[0]:
                                raise RuntimeError(
                                    "episode index is busy; retry recovery"
                                )
                            old.execute("PRAGMA journal_mode=DELETE")
                    except sqlite3.DatabaseError as exc:
                        if "not a database" not in str(exc) and "malformed" not in str(
                            exc
                        ):
                            raise
                        for suffix in ("-wal", "-shm"):
                            Path(str(self.path) + suffix).unlink(missing_ok=True)
                temporary.replace(self.path)
                terminal = sum(
                    value
                    for key, value in counts.items()
                    if key not in {"pending", "running"}
                )
                summary = {
                    "num_episodes": terminal,
                    "num_completed": counts.get("completed", 0),
                    "num_errors": counts.get("error", 0),
                    "num_cancelled": counts.get("cancelled", 0),
                    "successes": successes,
                    "episode_success_rate": successes / terminal if terminal else None,
                }
                for key, stats in adapters.items():
                    stats["success_rate"] = (
                        stats["successes"] / stats["num_episodes"]
                        if stats["num_episodes"]
                        else None
                    )
                    adapters[key] = {
                        **(
                            status.get("summary", {}).get("task_adapters", {}).get(key)
                            or {}
                        ),
                        **stats,
                    }
                summary["task_adapters"] = adapters
                status.update(
                    total_episodes=count,
                    completed_episodes=terminal,
                    summary={**(status.get("summary") or {}), **summary},
                    active=[],
                    current=None,
                )
                if status.get("status") in {None, "running", "starting"}:
                    status.update(
                        status="failed",
                        error="run interrupted; result index recovered from episode artifacts",
                    )
                write_json(status_path, status)
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    conn.execute("UPDATE index_meta SET value='1' WHERE key='clean'")
                return {
                    "indexed": count,
                    "counts": counts,
                    "summary": summary,
                    "path": str(self.path),
                }
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _row(record):
        return (
            record["adapter_id"],
            record["global_index"],
            record.get("updated_at") or datetime.now(timezone.utc).isoformat(),
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str),
        )

    def put(self, record):
        with closing(sqlite3.connect(self.path, timeout=1)) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO episodes VALUES (?,?,?,?)", self._row(record)
            )

    def page(self, *, limit=100, offset=0, since=None, until=None):
        limit, offset, since, until = page_window(limit, offset, since, until)
        where, args = [], []
        if since:
            where.append("updated_at>=?")
            args.append(since)
        if until:
            where.append("updated_at<=?")
            args.append(until)
        query = "SELECT record_json, updated_at FROM episodes"
        if where:
            query += " WHERE " + " AND ".join(where)
        result = TraceQuery(self.path).sql_page(
            query + " ORDER BY global_index, adapter_id LIMIT ? OFFSET ?",
            (*args, limit + 1, offset),
            limit=limit,
        )
        records = [
            {**json.loads(row["record_json"]), "updated_at": row["updated_at"]}
            for row in result.pop("rows")
        ]
        return {
            **result,
            "episodes": records,
            "offset": offset,
            "next_offset": offset + len(records) if result["truncated"] else None,
        }

    def get(self, adapter_id, global_index):
        rows = TraceQuery(self.path).sql(
            "SELECT record_json FROM episodes WHERE adapter_id=? AND global_index=?",
            (adapter_id, global_index),
        )
        return json.loads(rows[0]["record_json"]) if rows else None

    def contains_all(self, targets):
        identities = {(item["adapter_id"], item["global_index"]) for item in targets}
        found = set()
        values = list(identities)
        for start in range(0, len(values), 200):
            batch = values[start : start + 200]
            placeholders = ",".join("(?,?)" for _ in batch)
            rows = TraceQuery(self.path).sql(
                "SELECT adapter_id, global_index FROM episodes WHERE (adapter_id, global_index) IN ("
                + placeholders
                + ")",
                tuple(part for item in batch for part in item),
            )
            found.update((row["adapter_id"], row["global_index"]) for row in rows)
        return found == identities
