from __future__ import annotations

import asyncio
import inspect
import json
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ..contracts.serialization import jsonable
from ..schemas import TraceSpan

TraceSink = Callable[[list[TraceSpan]], Awaitable[None] | None]


# A TraceClient is shared by all spans in one run, while asyncio tasks need
# independent current parents for parallel graph nodes. ContextVar gives each
# task its own active span without putting live tracing state into the
# serializable GraphContext.
_active_span: ContextVar[tuple[object, UUID] | None] = ContextVar(
    "robotenon_active_trace_span", default=None
)


def trace_value(value: Any, *, max_text: int = 8000) -> Any:
    """Convert a model/node value into bounded JSON-safe trace attributes."""

    try:
        converted = jsonable(value)
    except (TypeError, ValueError):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"type": "bytes", "size_bytes": len(value)}
        shape = getattr(value, "shape", None)
        dtype = getattr(value, "dtype", None)
        if shape is not None or dtype is not None:
            return {
                "type": type(value).__name__,
                "shape": [int(item) for item in shape or ()],
                "dtype": str(dtype) if dtype is not None else None,
                "size_bytes": int(getattr(value, "nbytes", 0) or 0),
            }
        return str(value)[:max_text]
    if isinstance(converted, str) and len(converted) > max_text:
        return converted[:max_text] + "..."
    if isinstance(converted, Mapping):
        return {
            str(key): trace_value(item, max_text=max_text)
            for key, item in converted.items()
        }
    if isinstance(converted, list):
        return [trace_value(item, max_text=max_text) for item in converted]
    return converted


def trace_observation(
    feedback: Any = None,
    *,
    observation_view: Any = None,
    cache: Any = None,
) -> dict[str, Any]:
    """Return logical image/frame addresses without copying sensor payloads."""

    refs: dict[str, Any] = {}
    view_refs = getattr(observation_view, "refs", None)
    if isinstance(view_refs, Mapping):
        refs.update(view_refs)
    feedback_refs = getattr(feedback, "frames", None)
    if isinstance(feedback_refs, Mapping):
        refs.update(feedback_refs)

    frames: dict[str, dict[str, Any]] = {}
    image_uris: dict[str, str] = {}
    for name, ref in refs.items():
        uri = getattr(ref, "uri", None)
        if uri is None and isinstance(ref, Mapping):
            uri = ref.get("uri")
        if uri is None:
            continue
        item: dict[str, Any] = {"uri": str(uri)}
        for field in (
            "modality",
            "media_type",
            "encoding",
            "shape",
            "dtype",
            "sequence",
            "size_bytes",
        ):
            value = getattr(ref, field, None)
            if value is None and isinstance(ref, Mapping):
                value = ref.get(field)
            if value is not None:
                item[field] = trace_value(value)
        if cache is not None:
            describe = getattr(cache, "describe_uri", None)
            if callable(describe):
                try:
                    description = describe(str(uri))
                except Exception:
                    description = None
                if isinstance(description, Mapping):
                    storage_path = description.get("storage_path")
                    if storage_path:
                        item["storage_path"] = str(storage_path)
        frames[str(name)] = item
        if str(item.get("modality", "")).lower() == "rgb":
            image_uris[str(name)] = str(uri)
    return {"frames": frames, "image_uris": image_uris}


class UnixSocketTraceSink:
    """Async transport for a separately running ``Tenon traced``."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    async def __call__(self, batch: list[TraceSpan]) -> None:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            writer.write(
                json.dumps(
                    [span.model_dump(mode="json") for span in batch],
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=1.0)
            if not response or json.loads(response).get("ok") is not True:
                raise ConnectionError("observability batch was not acknowledged")
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                # close() releases the transport; a peer that disappeared
                # must not leave the producer task retained across episodes.
                pass


class TraceDaemon:
    """Resident append-only span store used independently from metrics."""

    def __init__(
        self,
        wal_path: str | Path | None = None,
        *,
        db_path: str | Path | None = None,
        max_recent: int = 10000,
    ):
        self.wal_path = Path(wal_path) if wal_path else None
        if self.wal_path:
            self.wal_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = (
            Path(db_path)
            if db_path
            else self.wal_path.with_suffix(".sqlite3")
            if self.wal_path
            else None
        )
        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            db_target = str(self.db_path)
        else:
            db_target = ":memory:"
        self._db = sqlite3.connect(db_target, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS spans (
               span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT,
               name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT,
               status TEXT NOT NULL, experiment_id TEXT, run_id TEXT, episode_id TEXT,
               attributes_json TEXT NOT NULL)"""
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id, start_time)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_spans_time ON spans(start_time)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_spans_episode ON spans(run_id, episode_id, start_time)"
        )
        self._db.commit()
        self._recent: deque[TraceSpan] = deque(maxlen=max_recent)
        self._lock = threading.Lock()
        self._discard_before: dict[tuple[str, str], datetime] = {}
        # JSONL is append-only; keep the last consumed byte so SQL/history
        # refreshes do not replay the complete trace archive every time.
        self._wal_offset = 0
        self.reload()

    def _insert_db(self, span: TraceSpan) -> None:
        self._db.execute(
            """INSERT OR REPLACE INTO spans(span_id,trace_id,parent_span_id,name,start_time,end_time,status,
               experiment_id,run_id,episode_id,attributes_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(span.span_id),
                str(span.trace_id),
                str(span.parent_span_id) if span.parent_span_id else None,
                span.name,
                span.start_time.isoformat(),
                span.end_time.isoformat() if span.end_time else None,
                span.status,
                span.experiment_id,
                str(span.run_id) if span.run_id else None,
                str(span.episode_id) if span.episode_id else None,
                json.dumps(
                    {
                        **span.attributes,
                        **{
                            key: str(getattr(span, key))
                            for key in (
                                "attempt_id",
                                "task_id",
                                "adapter_id",
                                "worker_id",
                                "gpu_id",
                            )
                            if getattr(span, key) is not None
                        },
                    },
                    default=str,
                ),
            ),
        )

    def reload(self) -> None:
        """Incrementally import JSONL spans written by another process."""
        if not self.wal_path or not self.wal_path.exists():
            return
        try:
            with self._lock:
                size = self.wal_path.stat().st_size
                if size < self._wal_offset:
                    # The WAL was rotated/truncated.  Rebuild the index from
                    # the replacement file rather than silently skipping it.
                    self._db.execute("DELETE FROM spans")
                    self._recent.clear()
                    self._wal_offset = 0
                with self.wal_path.open("r", encoding="utf-8") as handle:
                    handle.seek(self._wal_offset)
                    while True:
                        position = handle.tell()
                        line = handle.readline()
                        if not line:
                            break
                        if not line.endswith("\n"):
                            handle.seek(position)
                            break
                        self._wal_offset = handle.tell()
                        try:
                            self._insert_db(TraceSpan.model_validate_json(line))
                        except Exception:
                            continue
                    self._db.commit()
        except OSError:
            return

    def ingest(self, span: TraceSpan | dict[str, Any]) -> None:
        self.ingest_batch([span])

    def delete_episode(self, run_id: str, episode_id: str) -> None:
        """Remove replaced execution history from both the index and replay log.

        The coordinator must stop the episode's worker before calling this.
        Other episodes may continue writing through this store.
        """
        with self._lock:
            self._discard_before[(run_id, episode_id)] = datetime.now(timezone.utc)
            if not self._db.execute(
                "SELECT 1 FROM spans WHERE run_id=? AND episode_id=? LIMIT 1",
                (run_id, episode_id),
            ).fetchone():
                return
            if self.wal_path and self.wal_path.exists():
                temporary = self.wal_path.with_suffix(".purge.tmp")
                with self.wal_path.open() as source, temporary.open("w") as target:
                    for line in source:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            target.write(line)
                            continue
                        if (
                            row.get("run_id") != run_id
                            or row.get("episode_id") != episode_id
                        ):
                            target.write(line)
                temporary.replace(self.wal_path)
                self._wal_offset = self.wal_path.stat().st_size
            self._db.execute(
                "DELETE FROM spans WHERE run_id=? AND episode_id=?",
                (run_id, episode_id),
            )
            self._db.commit()
            self._recent = deque(
                (
                    span
                    for span in self._recent
                    if span.run_id != run_id or span.episode_id != episode_id
                ),
                maxlen=self._recent.maxlen,
            )

    def ingest_batch(self, spans: Iterable[TraceSpan | dict[str, Any]]) -> None:
        normalized = [
            span if isinstance(span, TraceSpan) else TraceSpan.model_validate(span)
            for span in spans
        ]
        if not normalized:
            return
        with self._lock:
            normalized = [
                span
                for span in normalized
                if (cutoff := self._discard_before.get((span.run_id, span.episode_id)))
                is None
                or span.start_time >= cutoff
            ]
            for span in normalized:
                self._recent.append(span)
                self._insert_db(span)
            self._db.commit()
            if self.wal_path:
                with self.wal_path.open("a", encoding="utf-8") as handle:
                    for span in normalized:
                        handle.write(span.model_dump_json() + "\n")
                    handle.flush()
                    self._wal_offset = handle.tell()

    def snapshot(self, *, limit: int = 1000) -> list[TraceSpan]:
        self.reload()
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM spans ORDER BY start_time DESC LIMIT ?", (limit,)
            ).fetchall()
        result: list[TraceSpan] = []
        for row in reversed(rows):
            result.append(
                TraceSpan(
                    trace_id=row["trace_id"],
                    span_id=row["span_id"],
                    parent_span_id=row["parent_span_id"],
                    name=row["name"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    status=row["status"],
                    experiment_id=row["experiment_id"],
                    run_id=row["run_id"],
                    episode_id=row["episode_id"],
                    attributes=json.loads(row["attributes_json"]),
                )
            )
        return result

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a read-only SQL query against the trace index."""
        statement = validate_trace_query(query)
        self.reload()
        with self._lock:
            rows = self._db.execute(statement, params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


class TraceClient:
    def __init__(
        self,
        sink: TraceSink | TraceDaemon | None = None,
        *,
        capacity: int = 4096,
        flush_interval: float = 0.2,
        trace_id: UUID | None = None,
        experiment_id: str | None = None,
        run_id: UUID | None = None,
        episode_id: UUID | str | None = None,
        identity: Mapping[str, Any] | None = None,
        mode: str = "full",
    ):
        if capacity <= 0:
            raise ValueError("trace client capacity must be positive")
        if flush_interval < 0:
            raise ValueError("trace client flush_interval cannot be negative")
        if mode not in {"full", "summary"}:
            raise ValueError("trace mode must be full or summary")
        self.mode = mode
        self.summary = {}
        self.summary_started = datetime.now(timezone.utc)
        self.sink = sink
        self.buffer: deque[TraceSpan] = deque(maxlen=capacity)
        self.capacity = capacity
        self.flush_interval = flush_interval
        self.trace_id = trace_id or uuid4()
        self.experiment_id = experiment_id
        self.run_id = run_id
        self.episode_id = episode_id
        self.identity = dict(identity or {})
        self.experiment_id = self.identity.get("experiment_id", self.experiment_id)
        self.run_id = self.identity.get("run_id", self.run_id)
        self.episode_id = self.identity.get("episode_id", self.episode_id)
        self.dropped = 0
        self.send_failures = 0
        self._buffer_lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._wake = asyncio.Event()

    def emit(self, span: TraceSpan) -> None:
        for key, value in self.identity.items():
            setattr(span, key, value)
        with self._buffer_lock:
            # An open span is emitted once at start and once again when it
            # closes. Replace a still-buffered copy so one logical span is
            # never duplicated merely because its lifecycle crossed a flush
            # boundary. TraceDaemon also upserts by span_id when the open
            # copy was already persisted.
            for index, buffered in enumerate(self.buffer):
                if buffered.span_id == span.span_id:
                    self.buffer[index] = span
                    return
            if len(self.buffer) >= self.capacity:
                self.buffer.popleft()
                self.dropped += 1
            self.buffer.append(span)

    @contextmanager
    def summary_span(self, name, *, attributes=None):
        """Count successful work without allocating/exporting per-step spans.

        Failed invocations still emit an individual span. Aggregate keys are
        bounded, and nested durations are explicitly not episode wall time.
        """
        started = datetime.now(timezone.utc)
        timer = time.perf_counter()
        current = TraceSpan(trace_id=self.trace_id, name=name, start_time=started,
                            attributes=dict(attributes or {}))
        try:
            yield current
        except BaseException as exc:
            current.status = "error"
            current.attributes.update({"error.type": type(exc).__name__, "error": str(exc)[:8000]})
            raise
        finally:
            elapsed = time.perf_counter() - timer
            key = name if name in self.summary or len(self.summary) < 256 else "__other__"
            item = self.summary.setdefault(key, {"count": 0, "errors": 0, "duration_sec": 0.0})
            item["count"] += 1
            item["duration_sec"] += elapsed
            if current.status == "error":
                item["errors"] += 1
                current.end_time = datetime.now(timezone.utc)
                self.emit(current)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        parent_span_id=None,
        attributes: dict[str, Any] | None = None,
    ):
        if self.mode == "summary":
            with self.summary_span(name, attributes=attributes) as current:
                yield current
            return
        start = datetime.now(timezone.utc)
        if parent_span_id is None:
            active = _active_span.get()
            if active is not None and active[0] is self:
                parent_span_id = active[1]
        current = TraceSpan(
            trace_id=self.trace_id,
            experiment_id=self.experiment_id,
            run_id=self.run_id,
            episode_id=self.episode_id,
            name=name,
            parent_span_id=parent_span_id,
            start_time=start,
            attributes=attributes or {},
        )
        token = _active_span.set((self, current.span_id))
        # Persist the open record immediately. The Portal maps a missing
        # end_time to a running bar; the final emit below updates this same
        # span_id with the completed status and end time.
        self.emit(current)
        try:
            yield current
        except Exception as exc:
            current.status = "error"
            current.attributes["error.type"] = type(exc).__name__
            raise
        finally:
            _active_span.reset(token)
            current.end_time = datetime.now(timezone.utc)
            if current.status == "unset":
                current.status = "ok"
            self.emit(current)

    @asynccontextmanager
    async def async_span(
        self,
        name: str,
        *,
        parent_span_id=None,
        attributes: dict[str, Any] | None = None,
    ):
        with self.span(
            name, parent_span_id=parent_span_id, attributes=attributes
        ) as current:
            yield current

    async def flush(self) -> int:
        if self.sink is None:
            return 0
        with self._buffer_lock:
            if not self.buffer:
                return 0
            batch = list(self.buffer)
            self.buffer.clear()
        sink = (
            self.sink.ingest_batch if isinstance(self.sink, TraceDaemon) else self.sink
        )
        try:
            if isinstance(self.sink, TraceDaemon):
                # SQLite commits and WAL appends are synchronous in the
                # resident daemon.  Flush them from a worker thread so trace
                # export never stalls an async workflow/control loop.
                await asyncio.to_thread(sink, batch)
            elif inspect.iscoroutinefunction(sink) or inspect.iscoroutinefunction(
                getattr(sink, "__call__", None)
            ):
                result = sink(batch)
                if inspect.isawaitable(result):
                    await result
            else:
                result = await asyncio.to_thread(sink, batch)
                if inspect.isawaitable(result):
                    await result
        except Exception:
            with self._buffer_lock:
                self.send_failures += 1
                for item in reversed(batch):
                    if len(self.buffer) < self.capacity:
                        self.buffer.appendleft(item)
                    else:
                        self.dropped += 1
            return 0
        return len(batch)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = None
        self._closed = False
        self._wake = asyncio.Event()

        async def loop() -> None:
            while not self._closed:
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=max(self.flush_interval, 0.001)
                    )
                except asyncio.TimeoutError:
                    pass
                await self.flush()

        self._task = asyncio.create_task(loop(), name="robotenon-trace-client")

    async def close(self) -> None:
        if self.summary:
            self.emit(TraceSpan(trace_id=self.trace_id, name="trace:summary",
                start_time=self.summary_started, end_time=datetime.now(timezone.utc), status="ok",
                attributes={"kind": "summary", "trace_mode": self.mode,
                            "duration_semantics": "sum of invocation durations; nested spans overlap",
                            "spans": dict(self.summary)}))
            self.summary.clear()
        self._closed = True
        if self._task:
            self._wake.set()
            await self._task
            self._task = None
        await self.flush()


def validate_trace_query(query: str) -> str:
    statement = query.strip().rstrip(";").strip()
    keyword = statement.split(None, 1)[0].lower() if statement else ""
    forbidden = re.search(
        r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|vacuum|reindex)\b",
        statement,
        re.IGNORECASE,
    )
    pragma_body = statement[len(keyword) :].strip() if keyword == "pragma" else ""
    # SQLite accepts mutating forms such as ``PRAGMA journal_mode=OFF``
    # and ``PRAGMA user_version=1``.  The web endpoint is intentionally
    # read-only, so only query-style introspection pragmas are allowed.
    readable_pragma = (
        bool(
            re.fullmatch(
                r"(?:(?:main|temp)\.)?(?:table_info|table_list|index_list|index_info|schema_version|user_version|foreign_keys|journal_mode|compile_options)(?:\s*\([^)]*\))?",
                pragma_body,
                re.IGNORECASE,
            )
        )
        if keyword == "pragma"
        else True
    )
    if (
        keyword not in {"select", "with", "pragma", "explain"}
        or ";" in statement
        or forbidden
        or (keyword == "pragma" and not readable_pragma)
    ):
        raise ValueError(
            "trace SQL endpoint accepts one read-only SELECT/WITH/PRAGMA/EXPLAIN statement"
        )
    return statement
