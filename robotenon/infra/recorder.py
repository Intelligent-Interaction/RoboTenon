from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Full, Queue
from typing import Any
from uuid import UUID, uuid4

from ..schemas import (
    Action,
    EnvironmentFeedback,
    EvaluationDecision,
    EventEnvelope,
    MetricSample,
    RunManifest,
    TraceSpan,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


class EpisodeRecorder:
    """Durable run artifact writer. Each record is append-only JSONL.

    Control-path calls only serialize a record and enqueue one small item. A
    daemon writer thread owns the file descriptors, so a slow filesystem or
    network-mounted run directory does not stall the workflow's action loop.
    ``finalize`` and ``close`` explicitly drain the queue for a durable run
    boundary; normal ``record_*`` calls remain non-blocking by default.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: UUID | None = None,
        queue_size: int = 8192,
        on_queue_full: str = "drop_oldest",
    ):
        if queue_size < 1:
            raise ValueError("recorder queue_size must be positive")
        if on_queue_full not in {"block", "drop_oldest", "drop_newest", "error"}:
            raise ValueError(
                "recorder on_queue_full must be block, drop_oldest, drop_newest or error"
            )
        self.root = Path(root)
        self.run_id = run_id or uuid4()
        self.events_path = self.root / "events.jsonl"
        self.actions_path = self.root / "actions.jsonl"
        self.feedback_path = self.root / "feedback.jsonl"
        self.decisions_path = self.root / "decisions.jsonl"
        self.metrics_path = self.root / "metrics.jsonl"
        self.trace_path = self.root / "trace.jsonl"
        self.manifest_path = self.root / "manifest.json"
        self.manifest: RunManifest | None = None
        self._started = False
        self._closed = False
        self.queue_size = queue_size
        self.on_queue_full = on_queue_full
        self._queue: Queue[tuple[Path, str] | None] = Queue(maxsize=queue_size)
        self._writer: threading.Thread | None = None
        self._writer_error: BaseException | None = None
        self._state_lock = threading.RLock()
        self.dropped = 0

    def start(self, *, session_id: UUID, task_id: str, environment_id: str, config_hash: str | None = None,
              metadata: dict[str, Any] | None = None) -> RunManifest:
        if self._started:
            raise RuntimeError("recorder.start() may only be called once per recorder")
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = RunManifest(
            run_id=self.run_id,
            session_id=session_id,
            task_id=task_id,
            environment_id=environment_id,
            started_at=datetime.now(timezone.utc),
            config_hash=config_hash,
            metadata=metadata or {},
        )
        self._write_manifest()
        self._started = True
        self._closed = False
        self._writer_error = None
        self._writer = threading.Thread(
            target=self._writer_loop,
            name=f"robotenon-recorder-{self.run_id}",
            daemon=True,
        )
        self._writer.start()
        return self.manifest

    def _append(self, path: Path, record: Any) -> None:
        if self._closed:
            raise RuntimeError("recorder is closed")
        if not self._started:
            raise RuntimeError("recorder.start() must be called first")
        line = json.dumps(
            record,
            default=_json_default,
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
        item = (path, line)
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except Full:
                if self.on_queue_full == "block":
                    self._queue.put(item)
                    return
                if self.on_queue_full == "drop_newest":
                    self.dropped += 1
                    return
                if self.on_queue_full == "error":
                    raise RuntimeError("recorder persistence queue is full")
                # ``drop_oldest`` keeps the newest control-path records while
                # preserving the bounded-memory guarantee.
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self.dropped += 1
                except Exception:
                    # A writer may have freed a slot between Full and get;
                    # retry the non-blocking put.
                    continue

    @staticmethod
    def _write_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    def _writer_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                path, line = item
                try:
                    self._write_line(path, line)
                except BaseException as exc:  # keep the resident writer alive
                    with self._state_lock:
                        if self._writer_error is None:
                            self._writer_error = exc
            finally:
                self._queue.task_done()

    def record_action(self, action: Action) -> None:
        self._append(self.actions_path, action)

    def record_feedback(self, feedback: EnvironmentFeedback) -> None:
        self._append(self.feedback_path, feedback)

    def record_decision(self, decision: EvaluationDecision) -> None:
        self._append(self.decisions_path, decision)

    def record_metric(self, sample: MetricSample) -> None:
        self._append(self.metrics_path, sample)

    def record_trace(self, span: TraceSpan) -> None:
        self._append(self.trace_path, span)

    def record_event(self, event: EventEnvelope) -> None:
        self._append(self.events_path, event)

    def finalize(self, *, status: str, success: bool | None, counters: dict[str, float] | None = None,
                 artifacts: list[Any] | None = None) -> RunManifest:
        if self.manifest is None:
            raise RuntimeError("recorder.start() must be called first")
        self.flush()
        if self._writer_error is not None:
            raise RuntimeError("recorder writer failed") from self._writer_error
        self.manifest.status = status  # type: ignore[assignment]
        self.manifest.success = success
        self.manifest.ended_at = datetime.now(timezone.utc)
        if counters:
            self.manifest.counters.update(counters)
        if artifacts:
            self.manifest.artifacts.extend(artifacts)
        if self.dropped:
            self.manifest.counters["recorder_dropped_records"] = float(self.dropped)
        self._write_manifest()
        return self.manifest

    def _write_manifest(self) -> None:
        assert self.manifest is not None
        temp = self.manifest_path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(self.manifest.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.manifest_path)
        # Portal run listings are served from a durable SQLite catalog. Keep
        # it current for newly-created and finalized manifests once a Portal
        # has bootstrapped that catalog. Index failures never affect recording.
        try:
            from .run_index import register_if_indexed

            register_if_indexed(self.manifest_path)
        except Exception:
            pass

    def flush(self, timeout: float | None = None) -> None:
        """Wait until all queued records are written.

        This method is intentionally explicit: callers use it at episode
        boundaries or before handoff, while the action/evaluation path uses
        only the non-blocking ``record_*`` methods.
        """

        if not self._started:
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for recorder persistence")
            time.sleep(0.002)
        if self._writer_error is not None:
            raise RuntimeError("recorder writer failed") from self._writer_error

    def close(self, timeout: float | None = None) -> None:
        """Drain and stop the writer thread; historical files are retained."""

        if not self._started or self._closed:
            return
        self.flush(timeout=timeout)
        self._queue.put(None)
        self._queue.join()
        if self._writer is not None:
            self._writer.join(timeout=timeout)
        self._writer = None
        self._closed = True
        self._started = False
