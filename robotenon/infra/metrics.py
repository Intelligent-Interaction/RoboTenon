from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..schemas import MetricSample

MetricSink = Callable[[list[MetricSample]], Awaitable[None] | None]

# Buckets are intentionally broad enough for the framework's common latency
# and age metrics (usually milliseconds), while still covering seconds-based
# custom metrics.  A metric can provide a tighter layout through
# ``MetricsClient.histogram(..., buckets=[...])`` or ``MetricDefinition``.
DEFAULT_HISTOGRAM_BUCKETS: tuple[float, ...] = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    25.0,
    50.0,
    75.0,
    100.0,
    150.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
    10000.0,
    30000.0,
    60000.0,
    300000.0,
    900000.0,
    3600000.0,
)
_PROMETHEUS_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _normalize_histogram_buckets(buckets: Iterable[float] | None) -> tuple[float, ...]:
    """Validate and normalize finite, strictly increasing bucket bounds."""
    if buckets is None:
        return DEFAULT_HISTOGRAM_BUCKETS
    values = tuple(float(item) for item in buckets)
    if not values or any(not math.isfinite(item) for item in values):
        raise ValueError("histogram buckets must contain finite values")
    normalized = tuple(sorted(set(values)))
    if normalized != values:
        raise ValueError("histogram buckets must be strictly increasing")
    return normalized


def histogram_quantile(
    phi: float, buckets: Mapping[float, float] | Iterable[tuple[float, float]]
) -> float | None:
    """Estimate a quantile from cumulative histogram bucket counts.

    ``buckets`` maps each inclusive upper bound (including ``math.inf`` for
    the implicit ``+Inf`` bucket) to its cumulative count.  The interpolation
    mirrors Prometheus' linear interpolation for classic histograms and is
    useful for local inspection/tests; time-windowed production queries
    should use PromQL ``histogram_quantile`` over the exported ``_bucket``
    series.
    """
    phi = float(phi)
    if not math.isfinite(phi) or not 0.0 <= phi <= 1.0:
        raise ValueError("phi must be between 0 and 1")
    items = list(buckets.items()) if isinstance(buckets, Mapping) else list(buckets)
    if not items:
        return None
    ordered = sorted((float(bound), float(count)) for bound, count in items)
    for index, (bound, count) in enumerate(ordered):
        if math.isnan(bound) or bound == -math.inf:
            raise ValueError("histogram bucket bounds must not be NaN or -Inf")
        if not math.isfinite(count) or count < 0:
            raise ValueError("histogram bucket counts must be finite and non-negative")
        if math.isinf(bound) and index != len(ordered) - 1:
            raise ValueError("+Inf must be the final histogram bucket")
    if not math.isinf(ordered[-1][0]):
        ordered.append((math.inf, ordered[-1][1]))
    previous_count = 0.0
    # Prometheus assumes zero as the lower bound for a first positive bucket.
    # For zero/negative-only layouts there is no finite lower bound to infer,
    # so returning the first upper bound is the conservative approximation.
    previous_bound = 0.0 if ordered[0][0] > 0 else ordered[0][0]
    total = ordered[-1][1]
    if total <= 0:
        return None
    for upper_bound, cumulative_count in ordered:
        if cumulative_count < previous_count:
            raise ValueError(
                "histogram bucket counts must be cumulative and non-decreasing"
            )
        if phi * total <= cumulative_count:
            if math.isinf(upper_bound):
                return previous_bound
            bucket_count = cumulative_count - previous_count
            if bucket_count <= 0:
                return upper_bound
            fraction = (phi * total - previous_count) / bucket_count
            return previous_bound + (upper_bound - previous_bound) * fraction
        previous_count = cumulative_count
        previous_bound = upper_bound
    return previous_bound


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: str
    kind: str
    unit: str | None = None
    allowed_labels: frozenset[str] = frozenset()
    histogram_buckets: tuple[float, ...] | None = None


class MetricRegistry:
    def __init__(self):
        self._definitions: dict[str, MetricDefinition] = {}

    def register(self, definition: MetricDefinition) -> None:
        if (
            definition.name in self._definitions
            and self._definitions[definition.name] != definition
        ):
            raise ValueError(
                f"metric already registered with a different definition: {definition.name}"
            )
        if definition.histogram_buckets is not None:
            if definition.kind != "histogram":
                raise ValueError(
                    "histogram_buckets can only be set for histogram metrics"
                )
            _normalize_histogram_buckets(definition.histogram_buckets)
        self._definitions[definition.name] = definition

    def definition(self, name: str) -> MetricDefinition | None:
        return self._definitions.get(name)

    def validate(self, sample: MetricSample) -> None:
        definition = self._definitions.get(sample.name)
        if definition is None:
            return
        if definition.kind != sample.kind:
            raise ValueError(
                f"metric kind mismatch for {sample.name}: expected {definition.kind}"
            )
        if definition.allowed_labels and not set(
            sample.labels or sample.tags or {}
        ).issubset(definition.allowed_labels):
            raise ValueError(f"metric contains an unregistered label: {sample.name}")
        if (
            definition.histogram_buckets is not None
            and sample.histogram_buckets is not None
        ):
            if _normalize_histogram_buckets(
                definition.histogram_buckets
            ) != _normalize_histogram_buckets(sample.histogram_buckets):
                raise ValueError(
                    f"histogram bucket boundaries do not match the definition for {sample.name}"
                )


class UnixSocketMetricsSink:
    """Async transport for a separately running ``Tenon metricsd``."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    async def __call__(self, batch: list[MetricSample]) -> None:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            writer.write(
                json.dumps(
                    [sample.model_dump(mode="json") for sample in batch],
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            await writer.drain()
            # The daemon acknowledges the batch. Do not wait forever on a broken collector.
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


class MetricsDaemon:
    """Resident metrics store.

    The daemon is deliberately transport-agnostic.  ``metrics_daemon.py``
    exposes it over a Unix socket; tests and in-process deployments can inject
    this object directly.  Writes are append-only JSONL so a collector can
    ship the WAL to Prometheus/OTel later without changing the client API.
    """

    def __init__(
        self,
        wal_path: str | Path | None = None,
        *,
        max_recent: int = 10000,
        registry: MetricRegistry | None = None,
        histogram_buckets: Mapping[str, Iterable[float]] | None = None,
        read_only: bool = False,
    ):
        self.wal_path = Path(wal_path) if wal_path else None
        self.read_only = read_only
        if self.wal_path and not read_only:
            self.wal_path.parent.mkdir(parents=True, exist_ok=True)
        self._recent: deque[MetricSample] = deque(maxlen=max_recent)
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]], tuple[float, int]
        ] = {}
        self._histogram_bucket_counts: dict[
            tuple[str, tuple[tuple[str, str], ...]], dict[float, int]
        ] = {}
        self._histogram_metric_buckets: dict[str, tuple[float, ...]] = {}
        self._configured_histogram_buckets = {
            name: _normalize_histogram_buckets(bounds)
            for name, bounds in (histogram_buckets or {}).items()
        }
        self._lock = threading.Lock()
        # Keep a byte offset into the append-only WAL.  Replaying the entire
        # file on every dashboard refresh makes latency grow with experiment
        # history and can contend with the control loop.  The offset is
        # advanced only over complete JSONL records, so a concurrent writer
        # that is still flushing a line is retried on the next refresh.
        self._wal_offset = 0
        self._wal_identity: tuple[int, int] | None = None
        self.registry = registry or MetricRegistry()
        self.reload()

    @staticmethod
    def _sample_labels(sample: MetricSample) -> dict[str, str]:
        # Keep the historical labels-over-tags precedence used by the daemon.
        return dict(sample.labels or sample.tags or {})

    def _resolve_histogram_buckets(self, sample: MetricSample) -> tuple[float, ...]:
        definition = self.registry.definition(sample.name)
        configured = self._configured_histogram_buckets.get(sample.name)
        defined = (
            _normalize_histogram_buckets(definition.histogram_buckets)
            if definition and definition.histogram_buckets
            else None
        )
        expected = configured or defined
        supplied = (
            _normalize_histogram_buckets(sample.histogram_buckets)
            if sample.histogram_buckets is not None
            else None
        )
        if expected is not None and supplied is not None and expected != supplied:
            raise ValueError(
                f"histogram bucket boundaries for {sample.name} do not match its registered/configured layout"
            )
        boundaries = supplied or expected or DEFAULT_HISTOGRAM_BUCKETS
        previous = self._histogram_metric_buckets.get(sample.name)
        if previous is not None and previous != boundaries:
            raise ValueError(
                f"histogram bucket boundaries for {sample.name} cannot change after first sample"
            )
        self._histogram_metric_buckets[sample.name] = boundaries
        return boundaries

    @staticmethod
    def _copy_with_histogram_buckets(
        sample: MetricSample, buckets: tuple[float, ...]
    ) -> MetricSample:
        # Keep the resolved layout in the WAL so a second metricsd process can
        # rebuild identical buckets without sharing constructor configuration.
        if hasattr(sample, "model_copy"):
            return sample.model_copy(update={"histogram_buckets": buckets})
        return sample.copy(update={"histogram_buckets": buckets})

    def _accept_memory(self, sample: MetricSample) -> None:
        labels = self._sample_labels(sample)
        key = (sample.name, tuple(sorted(labels.items())))
        if sample.kind == "counter":
            self._counters[key] = self._counters.get(key, 0.0) + sample.value
        elif sample.kind == "gauge":
            self._gauges[key] = sample.value
        elif sample.kind == "histogram":
            if not math.isfinite(sample.value):
                raise ValueError("histogram observations must be finite")
            boundaries = self._resolve_histogram_buckets(sample)
            total, count = self._histograms.get(key, (0.0, 0))
            self._histograms[key] = (total + sample.value, count + 1)
            buckets = self._histogram_bucket_counts.setdefault(
                key, {bound: 0 for bound in boundaries}
            )
            for bound in boundaries:
                if sample.value <= bound:
                    buckets[bound] = buckets.get(bound, 0) + 1
            # Prometheus classic histograms always include an implicit
            # cumulative +Inf bucket.
            buckets[math.inf] = buckets.get(math.inf, 0) + 1
        self._recent.append(sample)

    def reload(self) -> None:
        """Incrementally import records written by another metrics process."""
        if not self.wal_path:
            return
        with self._lock:
            try:
                with self.wal_path.open("r", encoding="utf-8") as handle:
                    stat = os.fstat(handle.fileno())
                    identity = (stat.st_dev, stat.st_ino)
                    if (
                        identity != self._wal_identity
                        or stat.st_size < self._wal_offset
                    ):
                        # Atomic replacement and truncation/rotation both
                        # require rebuilding state from the new file head.
                        self._recent.clear()
                        self._counters.clear()
                        self._gauges.clear()
                        self._histograms.clear()
                        self._histogram_bucket_counts.clear()
                        self._histogram_metric_buckets.clear()
                        self._wal_offset = 0
                    self._wal_identity = identity
                    handle.seek(self._wal_offset)
                    while True:
                        position = handle.tell()
                        line = handle.readline()
                        if not line:
                            break
                        if not line.endswith("\n"):
                            # Do not consume a partially-written JSON record.
                            handle.seek(position)
                            break
                        self._wal_offset = handle.tell()
                        try:
                            sample = MetricSample.model_validate_json(line)
                        except Exception:
                            continue
                        try:
                            self._accept_memory(sample)
                        except ValueError:
                            # A malformed observation must not make a resident
                            # exporter unavailable.  It is skipped during replay;
                            # live ingest still reports the validation error.
                            continue
            except OSError:
                return

    def ingest(self, sample: MetricSample | dict[str, Any]) -> None:
        self.ingest_batch([sample])

    def _normalize_sample(self, sample: MetricSample | dict[str, Any]) -> MetricSample:
        if not isinstance(sample, MetricSample):
            sample = MetricSample.model_validate(sample)
        labels = self._sample_labels(sample)
        invalid_labels = [
            key for key in labels if not _PROMETHEUS_LABEL_RE.fullmatch(str(key))
        ]
        if invalid_labels:
            raise ValueError(
                "metric label names must be Prometheus-compatible: "
                + ", ".join(repr(item) for item in invalid_labels)
            )
        self.registry.validate(sample)
        if sample.kind == "histogram" and sample.histogram_buckets is None:
            definition = self.registry.definition(sample.name)
            configured = self._configured_histogram_buckets.get(sample.name)
            defined = (
                _normalize_histogram_buckets(definition.histogram_buckets)
                if definition and definition.histogram_buckets
                else None
            )
            resolved = configured if configured is not None else defined
            if resolved is not None:
                sample = self._copy_with_histogram_buckets(sample, resolved)
        return sample

    def ingest_batch(self, samples: Iterable[MetricSample | dict[str, Any]]) -> None:
        if self.read_only:
            raise RuntimeError("metrics query source is read-only")
        # The shared ingest service owns this file. Serialize each batch under
        # one lock so concurrent producers cannot interleave its records.
        with self._lock:
            lines = []
            try:
                for sample in samples:
                    sample = self._normalize_sample(sample)
                    line = sample.model_dump_json() if self.wal_path else None
                    self._accept_memory(sample)
                    if line is not None:
                        lines.append(line + "\n")
            finally:
                # Preserve the old accepted-prefix behavior if a later sample
                # (or the input iterator) fails. Readers consume complete lines.
                if lines:
                    with self.wal_path.open("a", encoding="utf-8") as handle:
                        handle.write("".join(lines))
                        handle.flush()
                        self._wal_offset = handle.tell()
                        stat = os.fstat(handle.fileno())
                        self._wal_identity = (stat.st_dev, stat.st_ino)

    def snapshot(
        self, *, name: str | None = None, limit: int = 1000
    ) -> list[MetricSample]:
        self.reload()
        with self._lock:
            items = list(self._recent)
        if name:
            items = [item for item in items if item.name == name]
        return items[-limit:]

    def counter_value(
        self,
        name: str,
        labels: dict[str, str] | None = None,
        *,
        tags: dict[str, str] | None = None,
    ) -> float:
        self.reload()
        key = (name, tuple(sorted((labels or tags or {}).items())))
        with self._lock:
            return self._counters.get(key, 0.0)

    def prometheus_text(self) -> str:
        """Return Prometheus text exposition, including classic histogram buckets."""
        self.reload()
        lines: list[str] = []

        def label_text(labels: tuple[tuple[str, str], ...]) -> str:
            if not labels:
                return ""
            # Prometheus label values use Go-style escaping.  Environment and
            # user-provided dimension values must never be interpolated
            # verbatim into exposition text.
            encoded = []
            for key, value in labels:
                escaped = (
                    str(value)
                    .replace("\\", "\\\\")
                    .replace('"', '\\"')
                    .replace("\n", "\\n")
                )
                encoded.append(f'{key}="{escaped}"')
            return "{" + ",".join(encoded) + "}"

        with self._lock:
            counters = dict(self._counters)
        for (name, labels), value in sorted(counters.items()):
            lines.append(f"{name}{label_text(labels)} {value}")
        with self._lock:
            gauges = dict(self._gauges)
            histograms = dict(self._histograms)
            histogram_bucket_counts = {
                key: dict(value) for key, value in self._histogram_bucket_counts.items()
            }
        for (name, labels), value in sorted(gauges.items()):
            lines.append(f"{name}{label_text(labels)} {value}")
        for name in sorted({name for name, _labels in histograms}):
            lines.append(f"# TYPE {name} histogram")
        for (name, labels), (total, count) in sorted(histograms.items()):
            text = label_text(labels)
            bucket_counts = histogram_bucket_counts.get((name, labels), {})
            for bound in sorted(item for item in bucket_counts if not math.isinf(item)):
                bucket_labels = dict(labels)
                bucket_labels["le"] = format(bound, ".15g")
                lines.append(
                    f"{name}_bucket{label_text(tuple(sorted(bucket_labels.items())))} {bucket_counts[bound]}"
                )
            if math.inf in bucket_counts:
                bucket_labels = dict(labels)
                bucket_labels["le"] = "+Inf"
                lines.append(
                    f"{name}_bucket{label_text(tuple(sorted(bucket_labels.items())))} {bucket_counts[math.inf]}"
                )
            lines.append(f"{name}_sum{text} {total}")
            lines.append(f"{name}_count{text} {count}")
        return "\n".join(lines) + ("\n" if lines else "")

    def quantile(
        self,
        name: str,
        phi: float,
        labels: Mapping[str, str] | None = None,
        *,
        tags: Mapping[str, str] | None = None,
    ) -> float | None:
        """Estimate a cumulative quantile for one metric or a label subset.

        When *labels* is omitted, all series for ``name`` are merged.  When
        supplied, matching series may contain additional labels and are
        merged as long as they use the same bucket layout.  This method is
        intended for local diagnostics; dashboard queries should use
        ``histogram_quantile`` over a rate window for time-bounded results.
        """
        self.reload()
        requested = dict(labels or tags or {})
        merged: dict[float, float] = {}
        with self._lock:
            for (
                metric_name,
                label_items,
            ), buckets in self._histogram_bucket_counts.items():
                if metric_name != name:
                    continue
                actual = dict(label_items)
                if any(actual.get(key) != value for key, value in requested.items()):
                    continue
                for bound, count in buckets.items():
                    merged[bound] = merged.get(bound, 0.0) + float(count)
        return histogram_quantile(phi, merged) if merged else None

    def names(self) -> list[str]:
        self.reload()
        with self._lock:
            return sorted({sample.name for sample in self._recent})


class MetricsClient:
    """Best-effort, non-blocking producer for a resident MetricsDaemon."""

    def __init__(
        self,
        sink: MetricSink | MetricsDaemon | None = None,
        *,
        capacity: int = 4096,
        flush_interval: float = 0.2,
        identity: Mapping[str, Any] | None = None,
    ):
        if capacity <= 0:
            raise ValueError("metrics client capacity must be positive")
        if flush_interval < 0:
            raise ValueError("metrics client flush_interval cannot be negative")
        self.sink = sink
        self.identity = dict(identity or {})
        self.buffer: deque[MetricSample] = deque(maxlen=capacity)
        self.capacity = capacity
        self.flush_interval = flush_interval
        self.dropped = 0
        self.send_failures = 0
        # Metrics can be emitted by environment/monitor worker threads as well
        # as by the asyncio control loop.  Protect the bounded deque while
        # keeping emit() itself allocation-only and non-blocking.
        self._buffer_lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._wake = asyncio.Event()

    def emit(
        self,
        sample: MetricSample | None = None,
        *,
        name: str | None = None,
        value: float | None = None,
        kind: str = "gauge",
        labels: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
        session_id=None,
        run_id=None,
        **fields: Any,
    ) -> None:
        if sample is None:
            if name is None or value is None:
                raise ValueError("name and value are required when sample is omitted")
            sample = MetricSample(
                name=name,
                value=value,
                kind=kind,
                labels=labels or tags or {},
                session_id=session_id,
                run_id=run_id,
                **fields,
            )
        for key, field_value in self.identity.items():
            setattr(sample, key, field_value)
        with self._buffer_lock:
            if len(self.buffer) >= self.capacity:
                self.buffer.popleft()
                self.dropped += 1
            self.buffer.append(sample)

    def counter(self, name: str, value: float = 1.0, **kwargs: Any) -> None:
        self.emit(name=name, value=value, kind="counter", **kwargs)

    def gauge(self, name: str, value: float, **kwargs: Any) -> None:
        self.emit(name=name, value=value, kind="gauge", **kwargs)

    def histogram(
        self,
        name: str,
        value: float,
        *,
        buckets: Iterable[float] | None = None,
        **kwargs: Any,
    ) -> None:
        """Queue one histogram observation with optional reusable buckets.

        Bucket bounds are persisted with the sample when explicitly supplied,
        so another resident daemon can reconstruct the same quantile layout.
        """
        if buckets is not None:
            if "histogram_buckets" in kwargs:
                raise TypeError("pass either buckets or histogram_buckets, not both")
            kwargs["histogram_buckets"] = tuple(float(item) for item in buckets)
        self.emit(name=name, value=value, kind="histogram", **kwargs)

    async def flush(self) -> int:
        if self.sink is None:
            return 0
        with self._buffer_lock:
            if not self.buffer:
                return 0
            batch = list(self.buffer)
            self.buffer.clear()
        sink = (
            self.sink.ingest_batch
            if isinstance(self.sink, MetricsDaemon)
            else self.sink
        )
        try:
            if isinstance(self.sink, MetricsDaemon):
                # ``MetricsDaemon.ingest_batch`` appends to a WAL and updates
                # in-memory indexes synchronously.  Keep that filesystem work
                # off the event loop; the producer's control path only ever
                # touches the bounded deque above.
                await asyncio.to_thread(sink, batch)
            elif inspect.iscoroutinefunction(sink) or inspect.iscoroutinefunction(
                getattr(sink, "__call__", None)
            ):
                result = sink(batch)
                if inspect.isawaitable(result):
                    await result
            else:
                # A synchronous exporter (for example a small test sink or a
                # blocking HTTP client) must not run on the control event loop.
                result = await asyncio.to_thread(sink, batch)
                if inspect.isawaitable(result):
                    await result
        except Exception:
            # Restore only when capacity permits; control path remains best effort.
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

        self._task = asyncio.create_task(loop(), name="robotenon-metrics-client")

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._wake.set()
            await self._task
            self._task = None
        await self.flush()
