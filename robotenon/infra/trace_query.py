"""Bounded read-only trace queries; readers never replay WAL or create DBs."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..schemas import TraceSpan
from .trace import validate_trace_query


class QueryBudgetExceeded(ValueError):
    pass


class TraceQuery:
    def __init__(
        self,
        db_path: str | Path,
        *,
        timeout_s=0.5,
        max_rows=10000,
        max_bytes=3 * 1024 * 1024,
    ):
        if min(timeout_s, max_rows, max_bytes) <= 0:
            raise ValueError("query budgets must be positive")
        self.db_path = Path(db_path)
        self.timeout_s, self.max_rows, self.max_bytes = timeout_s, max_rows, max_bytes

    def sql_page(
        self, query: str, params: tuple[Any, ...] = (), *, limit=None, offset=0
    ):
        statement = validate_trace_query(query)
        limit = min(self.max_rows, self.max_rows if limit is None else int(limit))
        if limit < 1 or offset < 0 or offset > 100000:
            raise ValueError("invalid query page; offset must be between 0 and 100000")
        result = {
            "rows": [],
            "truncated": False,
            "next_offset": None,
            "offset": offset,
            "limit": limit,
        }
        if not self.db_path.is_file():
            return result
        deadline = time.monotonic() + self.timeout_s
        connection = sqlite3.connect(
            self.db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=self.timeout_s,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 1000
            )
            if hasattr(connection, "setlimit"):
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.max_bytes)
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024 * 1024)
            cursor = connection.execute(statement, params)
            size = 0
            for index, row in enumerate(cursor):
                if time.monotonic() >= deadline:
                    raise TimeoutError("trace query exceeded its time budget")
                if index < offset:
                    continue
                value = dict(row)
                row_size = len(
                    json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
                )
                if row_size > self.max_bytes:
                    raise QueryBudgetExceeded(
                        "one trace row exceeds the response byte budget"
                    )
                if len(result["rows"]) >= limit or size + row_size > self.max_bytes:
                    result.update(
                        truncated=True, next_offset=offset + len(result["rows"])
                    )
                    break
                result["rows"].append(value)
                size += row_size
            return result
        except sqlite3.OperationalError as exc:
            if time.monotonic() >= deadline or "interrupted" in str(exc):
                raise TimeoutError("trace query exceeded its time budget") from exc
            raise
        except sqlite3.DataError as exc:
            raise QueryBudgetExceeded(
                "trace query exceeds the SQLite value size budget"
            ) from exc
        finally:
            connection.close()

    def sql(
        self, query: str, params: tuple[Any, ...] = (), *, limit=None, offset=0
    ) -> list[dict[str, Any]]:
        page = self.sql_page(query, params, limit=limit, offset=offset)
        if page["truncated"] and limit is None:
            raise QueryBudgetExceeded(
                "trace result exceeds budget; use sql_page or an explicit limit"
            )
        return page["rows"]

    def episode_page(
        self,
        *,
        run_id: str | None = None,
        episode_id: str | None = None,
        attempt_id: str | None = None,
        limit=200,
        offset=0,
        since=None,
        until=None,
    ):
        from .history import page_window

        # Internal trajectory readers may use the larger trace row budget.
        _, offset, since, until = page_window(1, offset, since, until)
        if int(limit) < 1:
            raise ValueError("limit must be positive")
        filters, args = [], []
        for column, value in [("run_id", run_id), ("episode_id", episode_id)]:
            if value is not None:
                filters.append(column + "=?")
                args.append(str(value))
        if attempt_id is not None:
            filters.append("json_extract(attributes_json, '$.attempt_id')=?")
            args.append(str(attempt_id))
        for operator, value in [(">=", since), ("<=", until)]:
            if value is not None:
                filters.append("start_time" + operator + "?")
                args.append(value)
        query = "SELECT * FROM spans" + (
            " WHERE " + " AND ".join(filters) if filters else ""
        )
        # SQL OFFSET avoids materialising skipped rows in Python; both execution
        # and output remain subject to the same time/byte budgets.
        order = (
            " ORDER BY start_time, rowid"
            if run_id
            else " ORDER BY start_time DESC, rowid DESC"
        )
        page = self.sql_page(
            query + order + " LIMIT ? OFFSET ?",
            (*args, min(int(limit), self.max_rows) + 1, offset),
            limit=limit,
        )
        page.update(
            offset=offset,
            next_offset=offset + len(page["rows"]) if page["truncated"] else None,
        )
        return page

    def episode(
        self,
        *,
        run_id: str,
        episode_id: str,
        attempt_id: str | None = None,
        limit=10000,
        offset=0,
        since=None,
        until=None,
    ) -> list[dict[str, Any]]:
        page = self.episode_page(
            run_id=run_id,
            episode_id=episode_id,
            attempt_id=attempt_id,
            limit=limit,
            offset=offset,
            since=since,
            until=until,
        )
        if page["truncated"]:
            raise QueryBudgetExceeded(
                "episode trace exceeds budget; request a time window or page"
            )
        return page["rows"]

    def snapshot(self, *, limit: int = 1000) -> list[TraceSpan]:
        rows = self.sql(
            "SELECT * FROM spans ORDER BY start_time DESC LIMIT ?",
            (min(limit, self.max_rows),),
            limit=limit,
        )
        result = []
        for row in reversed(rows):
            attributes = json.loads(row.pop("attributes_json"))
            identity = {
                key: attributes[key]
                for key in (
                    "attempt_id",
                    "task_id",
                    "adapter_id",
                    "worker_id",
                    "gpu_id",
                )
                if key in attributes
            }
            result.append(TraceSpan(**row, attributes=attributes, **identity))
        return result

    def close(self) -> None:
        pass
