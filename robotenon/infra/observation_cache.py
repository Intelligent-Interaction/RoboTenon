"""Resident L0/L1/L2 observation cache.

``ObservationCache`` is deliberately independent from environment adapters.
It accepts canonical :class:`~robotenon.schemas.EnvironmentFeedback` records,
keeps a bounded hot ring in memory, and persists media in a background queue.
Only metadata and logical ``cache://`` URIs are retained in feedback/context;
large payloads are owned by a short-lived persistence job and the file store.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ..contracts.serialization import jsonable as _jsonable
from ..schemas import EnvironmentFeedback, ObservationChannel, ObservationRef
from .metrics import MetricsClient, MetricsDaemon
from .observation_store import FileStore, describe_payload

OBSERVATION_CACHE_METRIC_NAMES: tuple[str, ...] = (
    "observation_cache_append_total",
    "observation_cache_hit_total",
    "observation_cache_miss_total",
    "observation_cache_evicted_total",
    "observation_cache_persisted_total",
    "observation_cache_dropped_total",
    "observation_cache_resolve_latency_ms",
    "observation_cache_persist_latency_ms",
    "observation_cache_memory_bytes",
    "observation_cache_disk_bytes",
    "observation_cache_queue_depth",
    "observation_cache_stale_total",
)


class ObservationCacheClosedError(RuntimeError):
    """Raised when writes are attempted after :meth:`ObservationCache.close`."""


class ObservationPersistenceDropped(RuntimeError):
    """Raised for a durable append whose persistence job was dropped."""


@dataclass(slots=True)
class _PersistJob:
    feedback: EnvironmentFeedback
    payloads: dict[str, Any]
    future: asyncio.Future[None] | None = None


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        # The public cache URI contract permits human-readable session names
        # while the legacy RoboTenon schema uses UUIDs.  A deterministic UUID5
        # preserves the name for old consumers without losing the URI key.
        return uuid5(NAMESPACE_URL, f"robotenon:session:{value}")


def _as_feedback_uuid(value: Any) -> UUID:
    """Coerce a public feedback key to the deterministic SQLite key."""

    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return uuid5(NAMESPACE_URL, f"robotenon:feedback:{value}")


def _timestamp_ns(feedback: EnvironmentFeedback) -> int:
    if feedback.timestamp_ns is not None:
        return int(feedback.timestamp_ns)
    return int(feedback.timestamp.timestamp() * 1_000_000_000)


def _logical_session_id(feedback: EnvironmentFeedback) -> str:
    value = (feedback.metadata or {}).get("_cache_session_id")
    return str(value) if value else str(feedback.session_id)


def _logical_feedback_id(feedback: EnvironmentFeedback) -> str:
    """Return the public feedback key used in a logical cache URI.

    The legacy RoboTenon schema stores UUID feedback IDs.  Lightweight
    adapters and the cache URI examples often use human-readable keys such as
    ``feedback-102``; those are retained in metadata while the canonical
    model keeps its UUID field for backwards compatibility.
    """

    value = (feedback.metadata or {}).get("_cache_feedback_id")
    return str(value) if value else str(feedback.feedback_id)


def _session_keys(value: Any) -> tuple[str, str]:
    raw = str(value)
    return raw, str(_as_uuid(value))


def _modality_for_channel(modality: str) -> str:
    value = str(modality).lower()
    return {
        "proprio": "proprioception",
        "state": "state",
        "image": "rgb",
    }.get(value, value)


def _metadata_safe(value: Any) -> Any:
    """Return JSON-safe metadata without retaining sensor-sized buffers."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"omitted": True, "type": "bytes", "size_bytes": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _metadata_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_metadata_safe(item) for item in value]
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    if shape is not None or dtype is not None:
        nbytes = getattr(value, "nbytes", None)
        return {
            "omitted": True,
            "type": type(value).__name__,
            "shape": list(shape or ()),
            "dtype": str(dtype) if dtype is not None else None,
            "size_bytes": int(nbytes) if nbytes is not None else None,
        }
    return str(value)


def _snapshot_payload(value: Any) -> Any:
    """Detach mutable sensor buffers before handing them to the async writer.

    Environment adapters commonly reuse the NumPy arrays returned by a
    renderer on the next simulator step.  The cache persists in a background
    task, so retaining that array object would let a later render mutate the
    bytes that are eventually written to disk.  That produces valid but
    visibly corrupted PNG/JPEG previews while the live video (written before
    the next step) remains correct.
    """

    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    # Avoid importing NumPy at module import time.  The shape/dtype/nbytes
    # combination is specific enough for the array-like sensor buffers we
    # need to detach, while keeping the cache usable without NumPy installed.
    if (
        getattr(value, "shape", None) is not None
        and getattr(value, "dtype", None) is not None
        and callable(getattr(value, "copy", None))
    ):
        try:
            return value.copy()
        except Exception:
            pass
    return value


def _infer_modality_name(name: str) -> str:
    text = str(name).lower()
    if "depth" in text or "range" in text:
        return "depth"
    if any(token in text for token in ("pointcloud", "point_cloud", "pcd", "lidar")):
        return "pointcloud"
    if any(token in text for token in ("proprio", "joint", "state", "pose", "velocity", "effort")):
        return "proprioception"
    if any(token in text for token in ("text", "caption", "instruction")):
        return "text"
    if any(token in text for token in ("camera", "image", "rgb", "front", "wrist")):
        return "rgb"
    return "other"


def make_observation_uri(session_id: Any, epoch: int, feedback_id: Any, frame_name: str, *, scheme: str = "cache") -> str:
    """Build the stable logical URI used in feedback and service messages."""

    if not scheme or scheme != "cache":
        raise ValueError("the built-in observation cache currently supports the 'cache' URI scheme")
    feedback_component = str(feedback_id)
    if not feedback_component.startswith("feedback-"):
        feedback_component = f"feedback-{feedback_component}"
    return (
        f"cache://{quote(str(session_id), safe='')}/epoch-{int(epoch)}"
        f"/{quote(feedback_component, safe='')}/{quote(str(frame_name), safe='')}"
    )


def _parse_cache_uri(uri: str) -> tuple[str, int, str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "cache" or not parsed.netloc:
        raise ValueError(f"unsupported observation URI: {uri!r}")
    parts = [unquote(item) for item in parsed.path.split("/") if item]
    if len(parts) != 3 or not parts[0].startswith("epoch-") or not parts[1].startswith("feedback-"):
        raise ValueError(f"malformed cache observation URI: {uri!r}")
    try:
        epoch = int(parts[0][len("epoch-"):])
    except ValueError as exc:
        raise ValueError(f"malformed epoch in observation URI: {uri!r}") from exc
    return unquote(parsed.netloc), epoch, parts[1][len("feedback-"):], parts[2]


parse_observation_uri = _parse_cache_uri


class ObservationCache:
    """Thread-safe, resident observation cache with asynchronous persistence.

    The public methods are async so the same facade works for an in-process
    service and a future UDS/HTTP client.  L0 operations are tiny lock
    sections; file and SQLite work executes in worker threads and never runs
    on an environment control coroutine.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] = "./data/observations",
        *,
        config: Mapping[str, Any] | Any | None = None,
        data_root: str | os.PathLike[str] | None = None,
        index_path: str | os.PathLike[str] | None = None,
        max_records: int = 64,
        max_bytes: int = 268_435_456,
        ttl_seconds: float = 300.0,
        queue_size: int = 256,
        workers: int = 2,
        on_queue_full: str = "drop_oldest",
        on_persist_queue_full: str | None = None,
        async_persist: bool = True,
        keep_sessions: int | None = 20,
        keep_days: float | None = 30.0,
        uri_scheme: str = "cache",
        metrics: Any = None,
        file_store: FileStore | None = None,
    ) -> None:
        if config is not None:
            # Accept the same nested object used by config.json without
            # forcing callers to instantiate runtime.config models first.
            config_value = config.model_dump(mode="json") if hasattr(config, "model_dump") else dict(config)
            if isinstance(config_value, Mapping) and isinstance(
                config_value.get("observation_cache"), Mapping
            ):
                config_value = config_value["observation_cache"]
            storage = config_value.get("storage") if isinstance(config_value, Mapping) else None
            hot = config_value.get("hot") if isinstance(config_value, Mapping) else None
            persist = config_value.get("persist") if isinstance(config_value, Mapping) else None
            if isinstance(storage, Mapping):
                root = storage.get("root", root)
                index_path = storage.get("index", index_path)
            if isinstance(hot, Mapping):
                max_records = hot.get("max_records", max_records)
                max_bytes = hot.get("max_bytes", max_bytes)
                ttl_seconds = hot.get("ttl_seconds", ttl_seconds)
            if isinstance(persist, Mapping):
                async_persist = persist.get(
                    "async", persist.get("async_enabled", async_persist)
                )
                queue_size = persist.get("queue_size", queue_size)
                workers = persist.get("workers", workers)
                on_queue_full = persist.get(
                    "on_queue_full",
                    persist.get("on_persist_queue_full", on_queue_full),
                )
            retention = config_value.get("retention") if isinstance(config_value, Mapping) else None
            if isinstance(retention, Mapping):
                keep_sessions = retention.get("keep_sessions", keep_sessions)
                keep_days = retention.get("keep_days", keep_days)
        if data_root is not None:
            root = data_root
        if on_persist_queue_full is not None:
            on_queue_full = on_persist_queue_full
        if max_records <= 0 or max_bytes <= 0 or ttl_seconds <= 0:
            raise ValueError("max_records, max_bytes and ttl_seconds must be positive")
        if queue_size <= 0 or workers <= 0:
            raise ValueError("queue_size and workers must be positive")
        if keep_sessions is not None and int(keep_sessions) <= 0:
            raise ValueError("keep_sessions must be positive when configured")
        if keep_days is not None and float(keep_days) <= 0:
            raise ValueError("keep_days must be positive when configured")
        if on_queue_full not in {"block", "drop_oldest", "drop_newest", "error"}:
            raise ValueError("on_queue_full must be block, drop_oldest, drop_newest or error")
        if uri_scheme != "cache":
            raise ValueError("the built-in observation cache currently supports uri_scheme='cache'")

        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path) if index_path is not None else self.root / "index.sqlite3"
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_store = file_store or FileStore(self.root)
        self.max_records = int(max_records)
        self.max_bytes = int(max_bytes)
        self.ttl_seconds = float(ttl_seconds)
        self.queue_size = int(queue_size)
        self.worker_count = int(workers)
        self.on_queue_full = on_queue_full
        self.async_persist = bool(async_persist)
        self.keep_sessions = int(keep_sessions) if keep_sessions is not None else None
        self.keep_days = float(keep_days) if keep_days is not None else None
        self.uri_scheme = uri_scheme
        self.metrics = metrics
        # A daemon object performs synchronous WAL writes.  Adapt it to the
        # bounded producer so accounting never performs filesystem I/O on an
        # environment coroutine.  An injected MetricsClient stays owned by
        # the caller and is not closed by the cache.
        self._metrics_client = (
            MetricsClient(metrics) if isinstance(metrics, MetricsDaemon) else None
        )

        self._lock = threading.RLock()
        self._db_lock = threading.RLock()
        self._db = sqlite3.connect(str(self.index_path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        self._hot: OrderedDict[str, tuple[EnvironmentFeedback, float, int]] = OrderedDict()
        self._latest_hot: dict[tuple[str, int], str] = {}
        self._hot_bytes = 0
        self._last_sequence: dict[tuple[str, int], int] = {}
        self._pending: dict[str, tuple[Any, Path, ObservationRef]] = {}
        # Metadata for queued records is retained even when an episode reset
        # clears L0.  This keeps history/latest queries coherent until the
        # background writer commits the job to SQLite.
        self._pending_records: dict[str, EnvironmentFeedback] = {}
        # In-process predecessor values let a dropped reservation restore the
        # local monotonic counter.  Durable rows remain the cross-process
        # authority; this map only prevents a queue-full drop from poisoning a
        # live cache instance until its next restart.
        self._reservation_previous: dict[
            str, tuple[tuple[str, int], int | None, int]
        ] = {}
        self._queue: asyncio.Queue[_PersistJob | None] | None = None
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._reservation_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock: asyncio.Lock | None = None
        self._started = False
        self._closed = False
        self._accepting = True
        with self._db_lock:
            row = self._db.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM observation_frames").fetchone()
        self._disk_bytes = int(row[0] or 0) if row is not None else 0
        self._metric_values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def _create_schema(self) -> None:
        with self._db_lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS observation_records (
                    feedback_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp_ns INTEGER NOT NULL,
                    terminal INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    feedback_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observation_frames (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feedback_id TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    uri TEXT NOT NULL UNIQUE,
                    media_type TEXT,
                    encoding TEXT,
                    shape_json TEXT,
                    dtype TEXT,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    storage_path TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observation_session_seq
                    ON observation_records(session_id, epoch, sequence);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_observation_session_epoch_sequence
                    ON observation_records(session_id, epoch, sequence);
                -- A reservation is created before the asynchronous file/row
                -- persistence job.  It is the cross-process ordering
                -- authority, so two cache clients cannot both accept the
                -- same (session, epoch, sequence) key.
                CREATE TABLE IF NOT EXISTS observation_sequence_reservations (
                    session_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    feedback_id TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, epoch, sequence),
                    UNIQUE (feedback_id)
                );
                CREATE INDEX IF NOT EXISTS idx_observation_reservation_session
                    ON observation_sequence_reservations(session_id, epoch, sequence);
                CREATE INDEX IF NOT EXISTS idx_observation_feedback
                    ON observation_frames(feedback_id);
                """
            )
            # Permit opening an index created by an early prototype that had
            # the metadata columns but no denormalised feedback JSON.  New
            # records always populate the column; old rows need an
            # application-specific feedback reconstruction before they can be
            # returned by the high-level history/latest helpers.
            columns = {
                str(row[1])
                for row in self._db.execute("PRAGMA table_info(observation_records)").fetchall()
            }
            if "feedback_json" not in columns:
                self._db.execute(
                    "ALTER TABLE observation_records ADD COLUMN feedback_json TEXT NOT NULL DEFAULT '{}'"
                )
            self._db.commit()

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | Any, *, metrics: Any = None) -> ObservationCache:
        """Construct a cache directly from the ``observation_cache`` block."""

        return cls(config=config, metrics=metrics)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    def _metric(self, name: str, value: float = 1.0, *, kind: str = "counter", labels: Mapping[str, str] | None = None) -> None:
        label_items = tuple(sorted((str(key), str(item)) for key, item in (labels or {}).items()))
        with self._lock:
            metric_key = (name, label_items)
            if kind == "counter":
                self._metric_values[metric_key] = self._metric_values.get(metric_key, 0.0) + float(value)
            else:
                self._metric_values[metric_key] = float(value)
        sink = self._metrics_client or self.metrics
        if sink is None:
            return
        try:
            method = getattr(sink, kind, None)
            if callable(method):
                method(name, value, labels=dict(labels or {}))
            elif callable(getattr(sink, "emit", None)):
                sink.emit(name=name, value=value, kind=kind, labels=dict(labels or {}))
            elif callable(getattr(sink, "ingest", None)):
                # ``MetricsDaemon`` is a useful in-process sink but exposes
                # ``ingest`` rather than the producer-side counter/gauge API.
                # Keep this compatibility path best-effort; production
                # control loops should pass a non-blocking MetricsClient.
                sink.ingest(
                    {
                        "name": name,
                        "value": value,
                        "kind": kind,
                        "labels": dict(labels or {}),
                    }
                )
        except Exception:
            # Observability must not make environment I/O fail.
            return

    def metric_snapshot(self) -> dict[str, float]:
        """Return locally accumulated cache metrics for a health/UI endpoint."""

        with self._lock:
            result: dict[str, float] = {}
            for (name, labels), value in self._metric_values.items():
                suffix = "" if not labels else "{" + ",".join(f"{key}={item}" for key, item in labels) + "}"
                result[name + suffix] = value
            for name in OBSERVATION_CACHE_METRIC_NAMES:
                result.setdefault(name, 0.0)
            return result

    @staticmethod
    def _feedback_from(value: EnvironmentFeedback | Mapping[str, Any]) -> EnvironmentFeedback:
        if isinstance(value, EnvironmentFeedback):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("observation cache append expects EnvironmentFeedback or a mapping")
        raw = dict(value)
        original_session = raw.get("session_id")
        original_feedback = raw.get("feedback_id")
        frame_values = raw.get("frames")
        if isinstance(frame_values, Mapping) and any(
            not isinstance(item, ObservationRef)
            and not (
                isinstance(item, Mapping)
                and "uri" in item
                and "data" not in item
                and "payload" not in item
            )
            for item in frame_values.values()
        ):
            # Raw frame payloads are supplied separately to ``_frame_payloads``
            # below; nested Pydantic validation must not inspect ndarrays.
            raw.pop("frames", None)
        raw["session_id"] = _as_uuid(raw.get("session_id") or uuid4())
        if original_feedback is None:
            raw["feedback_id"] = uuid4()
        else:
            raw["feedback_id"] = _as_feedback_uuid(original_feedback)
        if "sequence_no" not in raw and "sequence" in raw:
            raw["sequence_no"] = raw["sequence"]
        raw.setdefault("sequence_no", 0)
        raw.setdefault("sequence", raw["sequence_no"])
        if original_session is not None:
            metadata = dict(raw.get("metadata") or {})
            try:
                UUID(str(original_session))
            except (ValueError, TypeError, AttributeError):
                metadata.setdefault("_cache_session_id", str(original_session))
            raw["metadata"] = metadata
        if original_feedback is not None:
            metadata = dict(raw.get("metadata") or {})
            try:
                UUID(str(original_feedback))
            except (ValueError, TypeError, AttributeError):
                metadata.setdefault("_cache_feedback_id", str(original_feedback))
            raw["metadata"] = metadata
        return EnvironmentFeedback.model_validate(raw)

    @staticmethod
    def _sequence(feedback: EnvironmentFeedback) -> int:
        # Existing adapters use sequence_no; cache-aware adapters may only
        # populate sequence.  Prefer the explicitly non-zero value.
        if feedback.sequence_no == 0 and feedback.sequence != 0:
            return int(feedback.sequence)
        return int(feedback.sequence_no)

    @staticmethod
    def _sanitize_feedback(feedback: EnvironmentFeedback) -> EnvironmentFeedback:
        """Strip non-serialisable/large values from the returned contract.

        The pipeline is the normal entry point, but callers can also use the
        cache directly.  Keeping this guard in the cache makes the invariant
        true at the infrastructure boundary as well: feedback/context hold
        metadata and refs, never an ndarray or raw byte buffer.
        """

        observation = feedback.observation
        channels = [
            channel.model_copy(
                update={"metadata": _metadata_safe(channel.metadata)}
            )
            for channel in observation.channels
        ]
        frames = [
            frame.model_copy(
                update={
                    "metadata": _metadata_safe(frame.metadata),
                    "artifact_ref": _metadata_safe(frame.artifact_ref)
                    if frame.artifact_ref is not None
                    else None,
                }
            )
            for frame in observation.frames
        ]
        signals = [
            signal.model_copy(
                update={
                    "value": _metadata_safe(signal.value),
                    "metadata": _metadata_safe(signal.metadata),
                }
            )
            for signal in feedback.signals
        ]
        error_value = _metadata_safe(feedback.error) if feedback.error is not None else None
        if error_value is not None and not isinstance(error_value, Mapping):
            error_value = {"value": error_value}
        return feedback.model_copy(
            update={
                "state": _metadata_safe(feedback.state),
                "info": _metadata_safe(feedback.info),
                "metadata": _metadata_safe(feedback.metadata),
                "error": error_value,
                "signals": signals,
                "observation": observation.model_copy(
                    update={
                        "state": _metadata_safe(observation.state),
                        "channels": channels,
                        "frames": frames,
                    }
                ),
            }
        )

    @staticmethod
    def _strip_feedback_extras(feedback: EnvironmentFeedback) -> EnvironmentFeedback:
        """Move Pydantic extra fields into bounded metadata."""

        extras = getattr(feedback, "model_extra", None)
        if extras is None:
            field_names = set(getattr(EnvironmentFeedback, "__fields__", {}))
            extras = {
                key: value
                for key, value in getattr(feedback, "__dict__", {}).items()
                if key not in field_names
            }
        if not extras:
            return feedback
        metadata = dict(feedback.metadata or {})
        metadata.setdefault("_adapter_extra", _metadata_safe(extras))
        field_names = set(
            getattr(EnvironmentFeedback, "model_fields", {})
            or getattr(EnvironmentFeedback, "__fields__", {})
        )
        data = {
            key: getattr(feedback, key)
            for key in field_names
            if hasattr(feedback, key)
        }
        data["metadata"] = metadata
        try:
            return EnvironmentFeedback.model_validate(data)
        except Exception:
            return feedback.model_copy(update={"metadata": metadata})

    def make_ref(
        self,
        *,
        feedback: EnvironmentFeedback,
        frame_name: str,
        payload: Any = None,
        modality: str = "other",
        media_type: str | None = None,
        encoding: str | None = None,
        shape: list[int] | None = None,
        dtype: str | None = None,
    ) -> ObservationRef:
        """Create a URI ref before persistence; no payload bytes are copied."""

        desc = describe_payload(payload, modality=modality, media_type=media_type, encoding=encoding)
        seq = self._sequence(feedback)
        fid = _logical_feedback_id(feedback)
        uri = make_observation_uri(_logical_session_id(feedback), feedback.epoch, fid, frame_name, scheme=self.uri_scheme)
        return ObservationRef(
            uri=uri,
            modality=_modality_for_channel(modality),
            media_type=desc.media_type,
            encoding=desc.encoding,
            shape=shape if shape is not None else desc.shape,
            dtype=dtype if dtype is not None else desc.dtype,
            timestamp_ns=_timestamp_ns(feedback),
            feedback_id=fid,
            sequence=seq,
            size_bytes=desc.size_bytes,
            metadata={"frame_name": str(frame_name), "extension": desc.extension},
        )

    def _frame_payloads(
        self,
        feedback: EnvironmentFeedback,
        payloads: Mapping[str, Any] | None,
    ) -> tuple[EnvironmentFeedback, dict[str, Any]]:
        raw_payloads = dict(payloads or {})
        refs = dict(feedback.frames or {})
        extra_values = getattr(feedback, "model_extra", None)
        if extra_values is None:
            known_fields = set(getattr(EnvironmentFeedback, "__fields__", {}))
            extra_values = {
                key: value
                for key, value in getattr(feedback, "__dict__", {}).items()
                if key not in known_fields
            }
        if extra_values:
            for name, value in extra_values.items():
                if name not in raw_payloads and not isinstance(
                    value, (str, int, float, bool, type(None))
                ):
                    raw_payloads[str(name)] = value
        # URI refs may carry producer metadata from an external process; keep
        # it bounded and serialisable before the record enters L0/SQLite.
        refs = {
            name: ref.model_copy(update={"metadata": _metadata_safe(ref.metadata)})
            for name, ref in refs.items()
        }
        # ``payloads`` is the convenient direct API.  Existing refs can be
        # passed in the same mapping and are not treated as file payloads.
        for name, payload in list(raw_payloads.items()):
            if isinstance(payload, ObservationRef):
                refs[name] = payload.model_copy(
                    update={"metadata": _metadata_safe(payload.metadata)}
                )
                raw_payloads.pop(name, None)
                continue
            # JSON callers (including a cross-process client) naturally send
            # an ObservationRef as a plain object rather than a Pydantic
            # instance.  Preserve that URI-only descriptor instead of
            # serialising the descriptor itself as an application/json frame.
            if (
                isinstance(payload, Mapping)
                and "uri" in payload
                and "data" not in payload
                and "payload" not in payload
            ):
                ref = ObservationRef.model_validate(payload)
                refs[name] = ref.model_copy(
                    update={"metadata": _metadata_safe(ref.metadata)}
                )
                raw_payloads.pop(name, None)
                continue
            descriptor = (
                payload
                if isinstance(payload, Mapping) and ("data" in payload or "payload" in payload)
                else None
            )
            value = (
                descriptor.get("data", descriptor.get("payload"))
                if descriptor is not None
                else payload
            )
            # The persistence queue outlives this call.  Snapshot mutable
            # arrays now, before the simulator can reuse its render buffer.
            value = _snapshot_payload(value)
            modality = str(descriptor.get("modality", _infer_modality_name(name))) if descriptor else _infer_modality_name(name)
            media_type = descriptor.get("media_type") if descriptor else None
            encoding = descriptor.get("encoding") if descriptor else None
            shape = descriptor.get("shape") if descriptor else None
            dtype = descriptor.get("dtype") if descriptor else None
            ref = self.make_ref(
                feedback=feedback,
                frame_name=name,
                payload=value,
                modality=modality,
                media_type=media_type,
                encoding=encoding,
                shape=shape,
                dtype=dtype,
            )
            refs[name] = ref
            raw_payloads[name] = value

        # Direct callers may put raw buffers in the legacy Observation.frames
        # list instead of passing ``payloads``.  Convert them here as well so
        # the cache API itself guarantees URI-only feedback.
        cleaned_observation_frames = []
        for frame in feedback.observation.frames:
            name = frame.camera_id or frame.frame_id
            if frame.data is not None:
                if name not in refs:
                    value = frame.data
                    value = _snapshot_payload(value)
                    ref = self.make_ref(
                        feedback=feedback,
                        frame_name=name,
                        payload=value,
                        modality=_modality_for_channel(frame.modality),
                        encoding=frame.encoding,
                        shape=frame.shape,
                    )
                    refs[name] = ref
                    raw_payloads[name] = value
                cleaned_observation_frames.append(
                    frame.model_copy(update={"data": None, "artifact_ref": refs[name].model_dump(mode="json")})
                )
            elif frame.artifact_ref and frame.artifact_ref.get("uri"):
                artifact = dict(frame.artifact_ref)
                allowed = {
                    "uri",
                    "modality",
                    "media_type",
                    "encoding",
                    "shape",
                    "dtype",
                    "timestamp_ns",
                    "feedback_id",
                    "sequence",
                    "sha256",
                    "size_bytes",
                    "metadata",
                }
                artifact = {key: value for key, value in artifact.items() if key in allowed}
                artifact.setdefault("uri", frame.artifact_ref["uri"])
                artifact.setdefault("modality", frame.modality)
                refs.setdefault(
                    name,
                    ObservationRef.model_validate(artifact).model_copy(
                        update={"metadata": _metadata_safe(artifact.get("metadata"))}
                    ),
                )
                cleaned_observation_frames.append(frame.model_copy(update={"data": None, "artifact_ref": refs[name].model_dump(mode="json")}))
            else:
                cleaned_observation_frames.append(frame.model_copy(update={"data": None}))

        for channel in feedback.observation.channels:
            if channel.uri and channel.name not in refs:
                refs[channel.name] = ObservationRef(
                    uri=channel.uri,
                    modality=channel.modality,
                    encoding=channel.encoding,
                    shape=channel.shape,
                    timestamp_ns=int(channel.timestamp.timestamp() * 1_000_000_000),
                    feedback_id=_logical_feedback_id(feedback),
                    sequence=self._sequence(feedback),
                    metadata=dict(channel.metadata),
                )

        updates: dict[str, Any] = {
            "frames": refs,
            "sequence": self._sequence(feedback),
            "timestamp_ns": _timestamp_ns(feedback),
        }
        # Keep old Observation consumers useful: channels contain URI and
        # metadata only, never a raw frame buffer.
        observation = feedback.observation
        channels = {channel.name: channel for channel in observation.channels}
        for name, ref in refs.items():
            channel = channels.get(name)
            if channel is None:
                channels[name] = ObservationChannel(
                    name=name,
                    modality=_modality_for_channel(ref.modality),
                    uri=ref.uri,
                    encoding=ref.encoding,
                    shape=ref.shape,
                    timestamp=feedback.timestamp,
                    frame_id=ref.feedback_id,
                    metadata={"media_type": ref.media_type, "dtype": ref.dtype, **dict(ref.metadata)},
                )
            else:
                channels[name] = channel.model_copy(update={"uri": ref.uri, "encoding": ref.encoding, "shape": ref.shape})
        updates["observation"] = observation.model_copy(
            update={"channels": list(channels.values()), "frames": cleaned_observation_frames}
        )
        return self._strip_feedback_extras(feedback.model_copy(update=updates)), raw_payloads

    async def start(self) -> None:
        if self._closed:
            raise ObservationCacheClosedError("observation cache is closed")
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self._started:
                return
            self._loop = asyncio.get_running_loop()
            self._queue = asyncio.Queue(maxsize=self.queue_size)
            if self._metrics_client is not None:
                await self._metrics_client.start()
            self._worker_tasks = [
                asyncio.create_task(self._worker(index), name=f"robotenon-observation-persist-{index}")
                for index in range(self.worker_count)
            ]
            self._started = True

    async def append(
        self,
        feedback: EnvironmentFeedback | Mapping[str, Any],
        payloads: Mapping[str, Any] | None = None,
        *,
        durable: bool = False,
    ) -> EnvironmentFeedback:
        """Append feedback to L0 and enqueue L1/L2 persistence.

        Unless ``durable=True`` this returns after the bounded in-memory write
        and queue operation.  The default ``drop_oldest`` policy keeps an
        environment loop responsive when a camera produces data faster than
        disk can absorb it.
        """

        if self._closed or not self._accepting:
            raise ObservationCacheClosedError("observation cache is closed")
        await self.start()
        raw_frames = feedback.get("frames") if isinstance(feedback, Mapping) else None
        raw_extra_payloads: dict[str, Any] = {}
        if isinstance(feedback, Mapping):
            known_fields = set(
                getattr(EnvironmentFeedback, "model_fields", {})
                or getattr(EnvironmentFeedback, "__fields__", {})
            )
            for name, value in feedback.items():
                if name not in known_fields and not isinstance(
                    value, (str, int, float, bool, type(None))
                ):
                    raw_extra_payloads[str(name)] = value
        canonical = self._feedback_from(feedback)
        payload_source = payloads
        if payload_source is None and isinstance(raw_frames, Mapping):
            payload_source = raw_frames
        elif payload_source is None and raw_extra_payloads:
            payload_source = raw_extra_payloads
        canonical, payload_map = self._frame_payloads(canonical, payload_source)
        canonical = self._sanitize_feedback(canonical)
        sid = str(canonical.session_id)
        epoch = int(canonical.epoch)
        sequence = self._sequence(canonical)
        key = (sid, epoch)
        fid = str(canonical.feedback_id)
        now = time.monotonic()
        with self._lock:
            existing = self._hot.get(fid)
            if existing is not None:
                self._metric("observation_cache_append_total")
                return existing[0]
            pending_existing = self._pending_records.get(fid)
            if pending_existing is not None:
                self._metric("observation_cache_append_total")
                return pending_existing
        # A record may already have left L0 while remaining durable in L2;
        # duplicate feedback IDs are still idempotent across that boundary.
        persisted = await asyncio.to_thread(self._query_feedback, fid)
        if persisted is not None:
            self._metric("observation_cache_append_total")
            return self._decode_feedback(persisted)
        # Reserve the ordering key synchronously in SQLite.  The reservation
        # is tiny compared with media persistence and closes the race between
        # independent cache processes.  File writes and the full feedback row
        # remain asynchronous below.
        await asyncio.to_thread(
            self._reserve_sequence,
            sid,
            epoch,
            sequence,
            fid,
        )
        with self._lock:
            existing = self._hot.get(fid)
            if existing is not None:
                self._metric("observation_cache_append_total")
                return existing[0]
            pending_existing = self._pending_records.get(fid)
            if pending_existing is not None:
                self._metric("observation_cache_append_total")
                return pending_existing
            last = self._last_sequence.get(key)
            # Ordering is already reserved atomically in SQLite above. Do not
            # repeat the check against this process-local max: two valid
            # concurrent appends (seq=1 and seq=2) may acquire their asyncio
            # locks in the opposite order even though the reservation order
            # was valid. The durable unique index/reservation table remains
            # the source of truth for conflicts.
            self._last_sequence[key] = max(sequence, last if last is not None else sequence)
            self._reservation_previous[fid] = (key, last, sequence)
            record_bytes = sum(int(ref.size_bytes or 0) for ref in canonical.frames.values())
            self._hot[fid] = (canonical, now, record_bytes)
            self._pending_records[fid] = canonical
            latest_id = self._latest_hot.get(key)
            latest_item = self._hot.get(latest_id) if latest_id is not None else None
            if latest_item is None or self._sequence(latest_item[0]) <= sequence:
                self._latest_hot[key] = fid
            self._hot_bytes += record_bytes
            self._prune_locked(now)
            for name, payload in payload_map.items():
                ref = canonical.frames.get(name)
                if ref is None:
                    continue
                extension = str(ref.metadata.get("extension", ".bin"))
                path = self.file_store.path_for(
                    session_id=_logical_session_id(canonical),
                    epoch=epoch,
                    sequence=sequence,
                    frame_name=name,
                    extension=extension,
                )
                self._pending[ref.uri] = (payload, path, ref)
            self._metric("observation_cache_memory_bytes", float(self._hot_bytes), kind="gauge")
            self._metric("observation_cache_queue_depth", float(self._queue.qsize() if self._queue is not None else 0), kind="gauge")
        self._metric("observation_cache_append_total")
        job = _PersistJob(canonical, payload_map)
        if durable or not self.async_persist:
            assert self._loop is not None
            job.future = self._loop.create_future()
        await self._enqueue(job)
        self._metric(
            "observation_cache_queue_depth",
            float(self._queue.qsize() if self._queue is not None else 0),
            kind="gauge",
        )
        if (durable or not self.async_persist) and job.future is not None:
            await job.future
            with self._lock:
                durable_item = self._hot.get(fid)
            if durable_item is not None:
                return durable_item[0]
            durable_row = await asyncio.to_thread(self._query_feedback, fid)
            if durable_row is not None:
                return self._decode_feedback(durable_row)
        return canonical

    def _prune_locked(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        while self._hot:
            first_id, (first_feedback, inserted, record_bytes) = next(iter(self._hot.items()))
            reason: str | None = None
            if current - inserted > self.ttl_seconds:
                reason = "ttl"
            elif len(self._hot) > self.max_records:
                reason = "records"
            elif self._hot_bytes > self.max_bytes:
                reason = "bytes"
            if reason is None:
                break
            self._hot.pop(first_id)
            self._hot_bytes = max(0, self._hot_bytes - record_bytes)
            key = (str(first_feedback.session_id), int(first_feedback.epoch))
            if self._latest_hot.get(key) == first_id:
                replacement = next(
                    (
                        item_id
                        for item_id, item in reversed(self._hot.items())
                        if str(item[0].session_id) == key[0] and int(item[0].epoch) == key[1]
                    ),
                    None,
                )
                if replacement is None:
                    self._latest_hot.pop(key, None)
                else:
                    self._latest_hot[key] = replacement
            self._metric("observation_cache_evicted_total", labels={"reason": reason})
            if reason == "ttl":
                self._metric("observation_cache_stale_total")

    async def _enqueue(self, job: _PersistJob) -> None:
        assert self._queue is not None
        if self.on_queue_full == "block":
            await self._queue.put(job)
            return
        try:
            self._queue.put_nowait(job)
            return
        except asyncio.QueueFull:
            pass
        if self.on_queue_full == "drop_oldest":
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                if isinstance(dropped, _PersistJob):
                    self._drop_job(dropped, "queue_full_oldest")
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                self._drop_job(job, "queue_full_newest")
            return
        if self.on_queue_full == "drop_newest":
            self._drop_job(job, "queue_full_newest")
            return
        # ``error`` is reported directly from this append call.  Cancel a
        # durable waiter's future rather than leaving an exception that no
        # coroutine will ever retrieve.
        self._metric("observation_cache_dropped_total", labels={"reason": "queue_full_error"})
        self._remove_pending_job(job)
        self._schedule_reservation_release(job.feedback)
        if job.future is not None and not job.future.done():
            job.future.cancel()
        raise asyncio.QueueFull

    def _drop_job(self, job: _PersistJob, reason: str) -> None:
        self._metric("observation_cache_dropped_total", labels={"reason": reason})
        self._remove_pending_job(job)
        self._schedule_reservation_release(job.feedback)
        if job.future is not None and not job.future.done():
            job.future.set_exception(ObservationPersistenceDropped(reason))

    def _remove_pending_job(self, job: _PersistJob) -> None:
        with self._lock:
            self._pending_records.pop(str(job.feedback.feedback_id), None)
            for ref in job.feedback.frames.values():
                self._pending.pop(ref.uri, None)

    async def _worker(self, index: int) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    return
                started = time.perf_counter()
                try:
                    await asyncio.to_thread(self._persist_sync, job)
                except Exception as exc:
                    self._remove_pending_job(job)
                    self._schedule_reservation_release(job.feedback)
                    if job.future is not None and not job.future.done():
                        job.future.set_exception(exc)
                    self._metric("observation_cache_dropped_total", labels={"reason": "persist_error"})
                else:
                    if job.future is not None and not job.future.done():
                        job.future.set_result(None)
                    self._metric("observation_cache_persisted_total")
                    self._metric(
                        "observation_cache_persist_latency_ms",
                        (time.perf_counter() - started) * 1000.0,
                        kind="histogram",
                    )
            finally:
                self._queue.task_done()
                self._metric(
                    "observation_cache_queue_depth",
                    float(self._queue.qsize()),
                    kind="gauge",
                )

    def _persist_sync(self, job: _PersistJob) -> None:
        feedback = job.feedback
        sid = str(feedback.session_id)
        storage_session_id = _logical_session_id(feedback)
        sequence = self._sequence(feedback)
        persisted_refs: dict[str, ObservationRef] = dict(feedback.frames)
        total_bytes = 0
        completed_uris: list[str] = []
        for name, payload in job.payloads.items():
            ref = persisted_refs.get(name)
            if ref is None:
                continue
            extension = str(ref.metadata.get("extension", ".bin"))
            destination = self.file_store.path_for(
                session_id=storage_session_id,
                epoch=feedback.epoch,
                sequence=sequence,
                frame_name=name,
                extension=extension,
            )
            stored = self.file_store.put(
                payload,
                destination,
                media_type=ref.media_type,
                encoding=ref.encoding,
                shape=ref.shape,
                dtype=ref.dtype,
            )
            total_bytes += stored.size_bytes
            persisted_refs[name] = ref.model_copy(
                update={"sha256": stored.sha256, "size_bytes": stored.size_bytes}
            )
            completed_uris.append(ref.uri)
            with self._lock:
                self._disk_bytes += stored.size_bytes
        if persisted_refs != feedback.frames:
            feedback = feedback.model_copy(update={"frames": persisted_refs})
        created = datetime.now(timezone.utc).isoformat()
        feedback_json = json.dumps(_jsonable(feedback), ensure_ascii=False, separators=(",", ":"))
        fid = str(feedback.feedback_id)
        with self._db_lock, self._db:
            self._db.execute(
                """INSERT INTO observation_records
                   (feedback_id, session_id, epoch, sequence, timestamp_ns, terminal, created_at, feedback_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(feedback_id) DO UPDATE SET
                     session_id=excluded.session_id,
                     epoch=excluded.epoch,
                     sequence=excluded.sequence,
                     timestamp_ns=excluded.timestamp_ns,
                     terminal=excluded.terminal,
                     feedback_json=excluded.feedback_json""",
                (fid, sid, int(feedback.epoch), sequence, _timestamp_ns(feedback), int(feedback.terminal), created, feedback_json),
            )
            for ref in persisted_refs.values():
                metadata = dict(ref.metadata or {})
                extension = str(metadata.get("extension", ".bin"))
                frame_name = metadata.get("frame_name", ref.feedback_id or "frame")
                path = self.file_store.path_for(
                    session_id=storage_session_id,
                    epoch=feedback.epoch,
                    sequence=sequence,
                    frame_name=frame_name,
                    extension=extension,
                )
                # A remote producer may have materialised the object on a
                # shared filesystem before sending URI-only metadata.  Treat
                # the deterministic destination as durable when it already
                # exists, while never putting a physical path in the URI.
                payload_name = name_for_ref(ref, persisted_refs)
                storage_path = str(path) if payload_name in job.payloads or path.exists() else None
                self._db.execute(
                    """INSERT OR IGNORE INTO observation_frames
                       (feedback_id, modality, uri, media_type, encoding, shape_json, dtype, sha256, size_bytes, storage_path, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        fid,
                        ref.modality,
                        ref.uri,
                        ref.media_type,
                        ref.encoding,
                        json.dumps(ref.shape) if ref.shape is not None else None,
                        ref.dtype,
                        ref.sha256,
                        ref.size_bytes,
                        storage_path,
                        created,
                    ),
                )
                # A metadata-only ref may have been inserted by a previous
                # process; update missing storage/checksum fields idempotently.
                self._db.execute(
                    """UPDATE observation_frames SET sha256=COALESCE(?, sha256), size_bytes=COALESCE(?, size_bytes),
                           storage_path=COALESCE(?, storage_path)
                       WHERE uri=?""",
                    (
                        ref.sha256,
                        ref.size_bytes,
                        storage_path,
                        ref.uri,
                    ),
                )
            # Once the durable row is committed, its unique
            # (session, epoch, sequence) index becomes the ordering authority;
            # the short-lived reservation is no longer needed.  Keeping only
            # in-flight reservations prevents an append-only table from
            # growing forever when retention is disabled.
            self._db.execute(
                "DELETE FROM observation_sequence_reservations WHERE feedback_id=?",
                (fid,),
            )
            self._db.commit()
        with self._lock:
            self._reservation_previous.pop(fid, None)
        try:
            self._retention_cleanup_sync()
        except Exception:
            # Retention is housekeeping; a lock or filesystem hiccup must not
            # turn an already committed observation into a failed append.
            self._metric(
                "observation_cache_dropped_total",
                labels={"reason": "retention_error"},
            )
        with self._lock:
            for uri in completed_uris:
                self._pending.pop(uri, None)
            hot_item = self._hot.get(fid)
            if hot_item is not None and persisted_refs != hot_item[0].frames:
                # Publish checksum/actual-size metadata back into L0 after
                # the worker finishes.  The environment already received the
                # URI, while later latest()/history() calls see the durable
                # ref without waiting for an L2 round trip.
                updated = hot_item[0].model_copy(update={"frames": persisted_refs})
                old_size = hot_item[2]
                new_size = sum(int(ref.size_bytes or 0) for ref in persisted_refs.values())
                self._hot[fid] = (updated, hot_item[1], new_size)
                self._hot_bytes += new_size - old_size
                self._metric(
                    "observation_cache_memory_bytes",
                    float(self._hot_bytes),
                    kind="gauge",
                )
                self._prune_locked()
            self._pending_records.pop(fid, None)
        if total_bytes:
            self._metric("observation_cache_disk_bytes", float(self._disk_bytes), kind="gauge")

    def _retention_cleanup_sync(self) -> None:
        """Delete expired/old sessions from L1/L2 in a persistence worker.

        Retention is deliberately executed outside the environment coroutine
        and is best-effort: a transient filesystem error must not invalidate a
        feedback that is already present in L0.  ``None`` disables either
        dimension, which is useful for append-only replay archives.
        """

        if self.keep_sessions is None and self.keep_days is None:
            return
        candidate_ids: set[str] = set()
        candidate_keys: set[tuple[str, int]] = set()
        cutoff = None
        if self.keep_days is not None:
            cutoff = (
                datetime.now(timezone.utc).timestamp() - self.keep_days * 86400.0
            )
        with self._db_lock:
            if cutoff is not None:
                # created_at is ISO-8601 UTC text; SQLite's datetime() handles
                # the format emitted by _persist_sync.
                rows = self._db.execute(
                    "SELECT feedback_id FROM observation_records "
                    "WHERE julianday(created_at) < julianday(?)",
                    (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),),
                ).fetchall()
                candidate_ids.update(str(row[0]) for row in rows)
            if self.keep_sessions is not None:
                sessions = self._db.execute(
                    "SELECT session_id FROM observation_records "
                    "GROUP BY session_id ORDER BY MAX(created_at) DESC"
                ).fetchall()
                old_sessions = [str(row[0]) for row in sessions[self.keep_sessions :]]
                if old_sessions:
                    placeholders = ",".join("?" for _ in old_sessions)
                    rows = self._db.execute(
                        f"SELECT feedback_id FROM observation_records WHERE session_id IN ({placeholders})",
                        tuple(old_sessions),
                    ).fetchall()
                    candidate_ids.update(str(row[0]) for row in rows)
            if not candidate_ids:
                return
            placeholders = ",".join("?" for _ in candidate_ids)
            key_rows = self._db.execute(
                f"SELECT session_id, epoch FROM observation_records WHERE feedback_id IN ({placeholders})",
                tuple(candidate_ids),
            ).fetchall()
            candidate_keys.update((str(row[0]), int(row[1])) for row in key_rows)
            rows = self._db.execute(
                f"SELECT storage_path, COALESCE(size_bytes, 0) FROM observation_frames "
                f"WHERE feedback_id IN ({placeholders}) AND storage_path IS NOT NULL",
                tuple(candidate_ids),
            ).fetchall()
            removed_bytes = sum(int(row[1] or 0) for row in rows)
            self._db.execute(
                f"DELETE FROM observation_frames WHERE feedback_id IN ({placeholders})",
                tuple(candidate_ids),
            )
            self._db.execute(
                f"DELETE FROM observation_records WHERE feedback_id IN ({placeholders})",
                tuple(candidate_ids),
            )
            # Sequence reservations are part of the durable ordering
            # contract.  Retention must remove them together with their
            # feedback rows, otherwise a later run that legitimately reuses
            # the sequence number would be rejected after restart.
            self._db.execute(
                f"DELETE FROM observation_sequence_reservations "
                f"WHERE feedback_id IN ({placeholders})",
                tuple(candidate_ids),
            )
            self._db.commit()
            empty_keys: set[tuple[str, int]] = set()
            for key in candidate_keys:
                present = self._db.execute(
                    "SELECT 1 FROM observation_records WHERE session_id=? AND epoch=? "
                    "UNION ALL SELECT 1 FROM observation_sequence_reservations "
                    "WHERE session_id=? AND epoch=? LIMIT 1",
                    (key[0], key[1], key[0], key[1]),
                ).fetchone()
                if present is None:
                    empty_keys.add(key)
        with self._lock:
            for fid in candidate_ids:
                item = self._hot.pop(fid, None)
                if item is not None:
                    self._hot_bytes = max(0, self._hot_bytes - item[2])
                    key = (str(item[0].session_id), int(item[0].epoch))
                    if self._latest_hot.get(key) == fid:
                        self._latest_hot.pop(key, None)
                self._pending_records.pop(fid, None)
                self._reservation_previous.pop(fid, None)
            for key in empty_keys:
                if not any(
                    (str(item[0].session_id), int(item[0].epoch)) == key
                    for item in self._hot.values()
                ) and not any(
                    (str(item.session_id), int(item.epoch)) == key
                    for item in self._pending_records.values()
                ):
                    self._last_sequence.pop(key, None)
            remaining_memory = self._hot_bytes
        for storage_path, _size in rows:
            self.file_store.delete(storage_path)
        if removed_bytes:
            with self._lock:
                self._disk_bytes = max(0, self._disk_bytes - removed_bytes)
            self._metric("observation_cache_disk_bytes", float(self._disk_bytes), kind="gauge")
        self._metric(
            "observation_cache_memory_bytes",
            float(remaining_memory),
            kind="gauge",
        )
        self._metric(
            "observation_cache_evicted_total",
            float(len(candidate_ids)),
            labels={"reason": "retention"},
        )

    async def latest(self, session_id: Any, epoch: int | None = None) -> EnvironmentFeedback | None:
        sid, db_sid = _session_keys(session_id)
        with self._lock:
            self._prune_locked()
            candidates: list[EnvironmentFeedback] = []
            keys = [key for key in self._latest_hot if key[0] in {sid, db_sid} and (epoch is None or key[1] == int(epoch))]
            for key in keys:
                item_id = self._latest_hot.get(key)
                if item_id in self._hot:
                    candidates.append(self._hot[item_id][0])
            candidates.extend(
                item
                for item in self._pending_records.values()
                if str(item.session_id) in {sid, db_sid}
                and (epoch is None or int(item.epoch) == int(epoch))
            )
            if candidates:
                return max(candidates, key=lambda item: (int(item.epoch), self._sequence(item)))
        row = await asyncio.to_thread(self._query_one, db_sid, epoch)
        return self._decode_feedback(row) if row else None

    async def history(
        self,
        session_id: Any,
        epoch: int | None = None,
        length: int = 4,
        interval: int = 1,
    ) -> list[EnvironmentFeedback]:
        if length <= 0 or interval <= 0:
            raise ValueError("length and interval must be positive")
        sid, db_sid = _session_keys(session_id)
        with self._lock:
            self._prune_locked()
            hot_records = [
                item[0]
                for item in self._hot.values()
                if str(item[0].session_id) in {sid, db_sid} and (epoch is None or int(item[0].epoch) == int(epoch))
            ]
            pending_records = [
                item
                for item in self._pending_records.values()
                if str(item.session_id) in {sid, db_sid}
                and (epoch is None or int(item.epoch) == int(epoch))
            ]
        # L0 may contain the newest pending records that are not in SQLite
        # yet.  Merge both layers and de-duplicate by feedback_id instead of
        # returning only the older database slice when the hot ring is small.
        rows = await asyncio.to_thread(
            self._query_many,
            db_sid,
            epoch,
            length * interval + len(hot_records) + len(pending_records),
        )
        decoded = [self._decode_feedback(row) for row in rows]
        merged = {str(item.feedback_id): item for item in decoded}
        merged.update({str(item.feedback_id): item for item in hot_records})
        merged.update({str(item.feedback_id): item for item in pending_records})
        records = list(merged.values())
        records.sort(key=lambda item: (int(item.epoch), self._sequence(item)))
        selected = records[-1 : -(length * interval + 1) : -interval]
        selected.reverse()
        return selected

    async def get_by_feedback_id(self, feedback_id: Any) -> EnvironmentFeedback | None:
        fid = str(feedback_id)
        with self._lock:
            item = self._hot.get(fid)
            if item is not None:
                return item[0]
            for feedback, _inserted, _size in self._hot.values():
                if _logical_feedback_id(feedback) == fid:
                    return feedback
            for feedback in self._pending_records.values():
                if str(feedback.feedback_id) == fid or _logical_feedback_id(feedback) == fid:
                    return feedback
        # Query both the human-readable URI key and the canonical UUID used
        # as the SQLite primary key.  The latter is deterministic for a
        # non-UUID adapter key, so restarts preserve the lookup contract.
        row = await asyncio.to_thread(self._query_feedback, fid)
        if row is None:
            row = await asyncio.to_thread(self._query_feedback, str(_as_feedback_uuid(feedback_id)))
        return self._decode_feedback(row) if row else None

    async def get_by_sequence(
        self,
        session_id: Any,
        sequence: int,
        *,
        epoch: int | None = None,
    ) -> EnvironmentFeedback | None:
        """Return one feedback by the ``(session, epoch, sequence)`` key."""

        sid, db_sid = _session_keys(session_id)
        with self._lock:
            for feedback, _inserted, _size in self._hot.values():
                if str(feedback.session_id) in {sid, db_sid} and self._sequence(feedback) == int(sequence) and (
                    epoch is None or int(feedback.epoch) == int(epoch)
                ):
                    return feedback
            for feedback in self._pending_records.values():
                if str(feedback.session_id) in {sid, db_sid} and self._sequence(feedback) == int(sequence) and (
                    epoch is None or int(feedback.epoch) == int(epoch)
                ):
                    return feedback
        row = await asyncio.to_thread(self._query_sequence, db_sid, epoch, int(sequence))
        return self._decode_feedback(row) if row else None

    async def query(
        self,
        session_id: Any,
        *,
        epoch: int | None = None,
        sequence: int | None = None,
        limit: int = 100,
    ) -> list[EnvironmentFeedback]:
        """Query metadata records without exposing persistence internals."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        if sequence is not None:
            item = await self.get_by_sequence(session_id, sequence, epoch=epoch)
            return [item] if item is not None else []
        _sid, db_sid = _session_keys(session_id)
        rows = await asyncio.to_thread(self._query_many_limit, db_sid, epoch, limit)
        return [self._decode_feedback(row) for row in rows]

    def describe_uri(self, uri: str) -> dict[str, Any] | None:
        """Return persisted or pending storage metadata for one cache URI."""

        row = self._query_frame(str(uri))
        if row is not None:
            return row
        with self._lock:
            pending = self._pending.get(str(uri))
        if pending is None:
            return None
        ref = pending[2]
        return {
            "uri": str(uri),
            "storage_path": str(pending[1]),
            "modality": ref.modality,
            "media_type": ref.media_type,
            "encoding": ref.encoding,
            "shape": ref.shape,
            "dtype": ref.dtype,
            "size_bytes": ref.size_bytes,
        }

    async def resolve(self, uri: str) -> Any:
        from .observation_resolver import ObservationResolver

        return await ObservationResolver(self).resolve(uri)

    async def resolve_ref(self, ref: ObservationRef) -> Any:
        from .observation_resolver import ObservationResolver

        return await ObservationResolver(self).resolve_ref(ref)

    async def resolve_feedback(self, feedback: EnvironmentFeedback) -> Any:
        from .observation_resolver import ObservationResolver

        return await ObservationResolver(self).resolve_feedback(feedback)

    async def read_uri_bytes(self, uri: str) -> tuple[bytes, dict[str, Any]]:
        started = time.perf_counter()
        try:
            return await self._read_uri_bytes_impl(uri)
        finally:
            self._metric(
                "observation_cache_resolve_latency_ms",
                (time.perf_counter() - started) * 1000.0,
                kind="histogram",
            )

    async def _read_uri_bytes_impl(self, uri: str) -> tuple[bytes, dict[str, Any]]:
        if uri.startswith("cache://"):
            row = await asyncio.to_thread(self._query_frame, uri)
            if row is not None and row["storage_path"]:
                path = Path(row["storage_path"])
                if path.exists():
                    self._metric("observation_cache_hit_total", labels={"layer": "disk", "modality": row["modality"]})
                    return await asyncio.to_thread(self.file_store.read, path), row
            with self._lock:
                pending = self._pending.get(uri)
            if pending is not None:
                # Resolve the in-flight payload directly from L0.  The
                # persistence worker continues independently; a model/tool
                # request should not wait for disk when bytes are already in
                # the resident cache.
                raw = await asyncio.to_thread(await_payload_sync, pending[0])
                self._metric(
                    "observation_cache_hit_total",
                    labels={"layer": "memory", "modality": pending[2].modality},
                )
                return raw, dict(pending[2].model_dump(mode="json"))
            # The writer commits SQLite before removing _pending. It may
            # finish between our first disk query and the memory lookup.
            # Re-query after a memory miss to close that handoff race.
            row = await asyncio.to_thread(self._query_frame, uri)
            if row is not None and row["storage_path"]:
                path = Path(row["storage_path"])
                if path.exists():
                    self._metric("observation_cache_hit_total", labels={"layer": "disk", "modality": row["modality"]})
                    return await asyncio.to_thread(self.file_store.read, path), row
            self._metric("observation_cache_miss_total", labels={"reason": "not_found"})
            raise FileNotFoundError(uri)
        # Non-cache URI: local path or URL.  Resolver handles decoding, but
        # raw access is useful to custom backends too.
        from .observation_store import payload_to_bytes

        return await asyncio.to_thread(payload_to_bytes, uri), {"uri": uri}

    async def clear(self, session_id: Any, epoch: int | None = None) -> None:
        # Finish queued jobs before deleting their index rows.  Otherwise a
        # worker that is already in flight could recreate a record after the
        # DELETE and violate the caller's clear contract.
        await self.flush()
        sid, db_sid = _session_keys(session_id)
        with self._lock:
            selected = [
                (fid, item[0])
                for fid, item in self._hot.items()
                if str(item[0].session_id) in {sid, db_sid} and (epoch is None or int(item[0].epoch) == int(epoch))
            ]
            for fid, _feedback in selected:
                _feedback, _inserted, size = self._hot.pop(fid)
                self._hot_bytes = max(0, self._hot_bytes - size)
                for ref in _feedback.frames.values():
                    self._pending.pop(ref.uri, None)
                self._pending_records.pop(fid, None)
                key = (str(_feedback.session_id), int(_feedback.epoch))
                if self._latest_hot.get(key) == fid:
                    self._latest_hot.pop(key, None)
            for key in list(self._last_sequence):
                if key[0] in {sid, db_sid} and (epoch is None or key[1] == int(epoch)):
                    self._last_sequence.pop(key, None)
            for fid, feedback in list(self._pending_records.items()):
                if str(feedback.session_id) in {sid, db_sid} and (
                    epoch is None or int(feedback.epoch) == int(epoch)
                ):
                    self._pending_records.pop(fid, None)
        # SQLite transactions and potentially thousands of file deletes are
        # maintenance work.  Keep both off the event loop: ``clear`` is often
        # called by reset/retention code while a monitor and a body watchdog
        # are still running.  The worker returns sizes as well as paths so the
        # in-memory disk gauge remains accurate even when a file was already
        # removed by an external retention process.
        rows = await asyncio.to_thread(self._clear_storage_sync, db_sid, epoch)
        if rows:
            await asyncio.gather(
                *(
                    asyncio.to_thread(self.file_store.delete, path)
                    for path, _size in rows
                )
            )
            removed_bytes = sum(int(size or 0) for _path, size in rows)
            with self._lock:
                self._disk_bytes = max(0, self._disk_bytes - removed_bytes)
                remaining_memory = self._hot_bytes
            self._metric(
                "observation_cache_disk_bytes",
                float(self._disk_bytes),
                kind="gauge",
            )
        else:
            with self._lock:
                remaining_memory = self._hot_bytes
        self._metric(
            "observation_cache_memory_bytes",
            float(remaining_memory),
            kind="gauge",
        )

    async def clear_hot(self, session_id: Any, epoch: int | None = None) -> None:
        """Drop only L0 records while retaining durable history in L2.

        ``ObservationPipeline`` uses this at an episode reset so the new
        epoch cannot accidentally read frames from the previous hot window.
        Pending persistence jobs are intentionally left intact; an old epoch
        still has to reach disk and remain replayable.
        """

        sid, db_sid = _session_keys(session_id)
        with self._lock:
            selected = [
                (fid, item[0])
                for fid, item in self._hot.items()
                if str(item[0].session_id) in {sid, db_sid}
                and (epoch is None or int(item[0].epoch) == int(epoch))
            ]
            for fid, feedback in selected:
                _feedback, _inserted, size = self._hot.pop(fid)
                self._hot_bytes = max(0, self._hot_bytes - size)
                key = (str(feedback.session_id), int(feedback.epoch))
                if self._latest_hot.get(key) == fid:
                    self._latest_hot.pop(key, None)
            self._metric(
                "observation_cache_memory_bytes", float(self._hot_bytes), kind="gauge"
            )

    async def flush(self, timeout: float | None = None) -> None:
        if self._queue is None:
            return
        waiter = self._queue.join()
        if timeout is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout=timeout)

    async def close(self) -> None:
        if self._closed:
            return
        self._accepting = False
        if self._queue is not None:
            await self.flush()
            for _ in self._worker_tasks:
                await self._queue.put(None)
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks = []
        if self._metrics_client is not None:
            await self._metrics_client.close()
        if self._reservation_cleanup_tasks:
            await asyncio.gather(
                *list(self._reservation_cleanup_tasks), return_exceptions=True
            )
            self._reservation_cleanup_tasks.clear()
        self._started = False
        self._closed = True
        with self._db_lock:
            self._db.commit()
            self._db.close()
        with self._lock:
            self._hot.clear()
            self._latest_hot.clear()
            self._pending.clear()
            self._pending_records.clear()
            self._reservation_previous.clear()
            self._hot_bytes = 0

    async def stats(self) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            hot_records = len(self._hot)
            hot_bytes = self._hot_bytes
            pending = max(len(self._pending), len(self._pending_records))
        queue_depth = self._queue.qsize() if self._queue is not None else 0
        self._metric("observation_cache_memory_bytes", float(hot_bytes), kind="gauge")
        self._metric("observation_cache_disk_bytes", float(self._disk_bytes), kind="gauge")
        self._metric("observation_cache_queue_depth", float(queue_depth), kind="gauge")
        return {
            "hot_records": hot_records,
            "memory_bytes": hot_bytes,
            "disk_bytes": self._disk_bytes,
            "queue_depth": queue_depth,
            "pending": pending,
        }

    def _query_one(self, sid: str, epoch: int | None) -> tuple[Any, ...] | None:
        with self._db_lock:
            if epoch is None:
                return self._db.execute(
                    "SELECT feedback_json FROM observation_records WHERE session_id=? ORDER BY epoch DESC, sequence DESC LIMIT 1",
                    (sid,),
                ).fetchone()
            return self._db.execute(
                "SELECT feedback_json FROM observation_records WHERE session_id=? AND epoch=? ORDER BY sequence DESC LIMIT 1",
                (sid, int(epoch)),
            ).fetchone()

    def _query_many(self, sid: str, epoch: int | None, count: int) -> list[tuple[Any, ...]]:
        with self._db_lock:
            if epoch is None:
                return self._db.execute(
                    """
                    SELECT feedback_json
                    FROM (
                        SELECT feedback_json, epoch, sequence
                        FROM observation_records
                        WHERE session_id=?
                        ORDER BY epoch DESC, sequence DESC
                        LIMIT ?
                    )
                    ORDER BY epoch ASC, sequence ASC
                    """,
                    (sid, int(count)),
                ).fetchall()
            return self._db.execute(
                """
                SELECT feedback_json
                FROM (
                    SELECT feedback_json, sequence
                    FROM observation_records
                    WHERE session_id=? AND epoch=?
                    ORDER BY sequence DESC
                    LIMIT ?
                )
                ORDER BY sequence ASC
                """,
                (sid, int(epoch), int(count)),
            ).fetchall()

    def _query_feedback(self, fid: str) -> tuple[Any, ...] | None:
        with self._db_lock:
            return self._db.execute("SELECT feedback_json FROM observation_records WHERE feedback_id=?", (fid,)).fetchone()

    def _query_sequence(self, sid: str, epoch: int | None, sequence: int) -> tuple[Any, ...] | None:
        with self._db_lock:
            if epoch is None:
                return self._db.execute(
                    "SELECT feedback_json FROM observation_records WHERE session_id=? AND sequence=? ORDER BY epoch DESC LIMIT 1",
                    (sid, sequence),
                ).fetchone()
            return self._db.execute(
                "SELECT feedback_json FROM observation_records WHERE session_id=? AND epoch=? AND sequence=?",
                (sid, int(epoch), sequence),
            ).fetchone()

    def _query_last_sequence(self, sid: str, epoch: int) -> int | None:
        with self._db_lock:
            row = self._db.execute(
                """
                SELECT MAX(sequence) FROM (
                    SELECT sequence FROM observation_records
                    WHERE session_id=? AND epoch=?
                    UNION ALL
                    SELECT sequence FROM observation_sequence_reservations
                    WHERE session_id=? AND epoch=?
                )
                """,
                (sid, int(epoch), sid, int(epoch)),
            ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def _release_sequence(self, feedback_id: Any) -> None:
        """Release an in-flight ordering reservation after a dropped job.

        A committed observation keeps its durable unique index and therefore
        must not lose ordering protection when a duplicate cleanup races the
        writer.  The conditional delete is idempotent across cache processes.
        """

        fid = str(feedback_id)
        with self._db_lock:
            durable = self._db.execute(
                "SELECT 1 FROM observation_records WHERE feedback_id=?",
                (fid,),
            ).fetchone()
            if durable is None:
                self._db.execute(
                    "DELETE FROM observation_sequence_reservations WHERE feedback_id=?",
                    (fid,),
                )
            self._db.commit()
        # A local append records its predecessor before publishing the hot
        # item.  If its durable job was dropped, roll back only that newest
        # local reservation; a later append may already have advanced the
        # counter and must remain untouched.
        with self._lock:
            previous = self._reservation_previous.pop(fid, None)
            if previous is None or durable is not None:
                return
            key, prior_last, sequence = previous
            current = self._last_sequence.get(key)
            if current != sequence:
                return
            if prior_last is None:
                self._last_sequence.pop(key, None)
            else:
                self._last_sequence[key] = prior_last

    def _schedule_reservation_release(self, feedback: EnvironmentFeedback) -> None:
        """Release a reservation without putting SQLite I/O on the loop."""

        loop = self._loop
        if loop is None or not loop.is_running():
            try:
                self._release_sequence(feedback.feedback_id)
            except Exception:
                return
            return
        task = asyncio.create_task(
            asyncio.to_thread(self._release_sequence, feedback.feedback_id),
            name="robotenon-observation-release-reservation",
        )
        self._reservation_cleanup_tasks.add(task)
        task.add_done_callback(self._reservation_cleanup_tasks.discard)

    def _reserve_sequence(
        self,
        sid: str,
        epoch: int,
        sequence: int,
        fid: str,
    ) -> None:
        """Atomically reserve a feedback ordering key across processes."""

        now = datetime.now(timezone.utc).isoformat()
        with self._db_lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                by_feedback = self._db.execute(
                    "SELECT session_id, epoch, sequence FROM observation_sequence_reservations WHERE feedback_id=?",
                    (fid,),
                ).fetchone()
                if by_feedback is not None:
                    if (
                        str(by_feedback[0]) == sid
                        and int(by_feedback[1]) == int(epoch)
                        and int(by_feedback[2]) == int(sequence)
                    ):
                        self._db.commit()
                        return
                    raise ValueError(
                        f"feedback_id {fid!r} was already reserved for a different observation key"
                    )
                existing = self._db.execute(
                    "SELECT feedback_id FROM observation_sequence_reservations WHERE session_id=? AND epoch=? AND sequence=?",
                    (sid, int(epoch), int(sequence)),
                ).fetchone()
                if existing is None:
                    existing = self._db.execute(
                        "SELECT feedback_id FROM observation_records WHERE session_id=? AND epoch=? AND sequence=?",
                        (sid, int(epoch), int(sequence)),
                    ).fetchone()
                if existing is not None:
                    if str(existing[0]) == fid:
                        self._db.commit()
                        return
                    raise ValueError(
                        "observation sequence conflict for "
                        f"session={sid!r}, epoch={epoch}, sequence={sequence}"
                    )
                last = self._db.execute(
                    """
                    SELECT MAX(sequence) FROM (
                        SELECT sequence FROM observation_records
                        WHERE session_id=? AND epoch=?
                        UNION ALL
                        SELECT sequence FROM observation_sequence_reservations
                        WHERE session_id=? AND epoch=?
                    )
                    """,
                    (sid, int(epoch), sid, int(epoch)),
                ).fetchone()
                if last is not None and last[0] is not None and int(sequence) <= int(last[0]):
                    raise ValueError(
                        "observation sequence must be strictly increasing for "
                        f"session={sid!r}, epoch={epoch}: {sequence} <= {int(last[0])}"
                    )
                self._db.execute(
                    "INSERT INTO observation_sequence_reservations(session_id, epoch, sequence, feedback_id, reserved_at) VALUES (?, ?, ?, ?, ?)",
                    (sid, int(epoch), int(sequence), fid, now),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def _query_many_limit(self, sid: str, epoch: int | None, limit: int) -> list[tuple[Any, ...]]:
        with self._db_lock:
            if epoch is None:
                return self._db.execute(
                    "SELECT feedback_json FROM observation_records WHERE session_id=? ORDER BY epoch ASC, sequence ASC LIMIT ?",
                    (sid, int(limit)),
                ).fetchall()
            return self._db.execute(
                "SELECT feedback_json FROM observation_records WHERE session_id=? AND epoch=? ORDER BY sequence ASC LIMIT ?",
                (sid, int(epoch), int(limit)),
            ).fetchall()

    def _query_frame(self, uri: str) -> dict[str, Any] | None:
        with self._db_lock:
            row = self._db.execute(
                "SELECT modality, uri, media_type, encoding, shape_json, dtype, sha256, size_bytes, storage_path FROM observation_frames WHERE uri=?",
                (uri,),
            ).fetchone()
        if row is None:
            return None
        keys = ("modality", "uri", "media_type", "encoding", "shape_json", "dtype", "sha256", "size_bytes", "storage_path")
        result = dict(zip(keys, row))
        if result["shape_json"]:
            try:
                result["shape"] = json.loads(result["shape_json"])
            except (TypeError, ValueError):
                pass
        return result

    def _clear_storage_sync(
        self, sid: str, epoch: int | None
    ) -> list[tuple[Path, int]]:
        """Delete one session/epoch from SQLite and return files to remove.

        This method is deliberately synchronous and private: callers must run
        it with :func:`asyncio.to_thread`.  The ``BEGIN IMMEDIATE`` protects
        the read--delete pair from a concurrent persistence worker, while the
        context manager rollback keeps the index intact if a statement fails.
        """

        with self._db_lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if epoch is None:
                    rows = self._db.execute(
                        "SELECT f.storage_path, COALESCE(f.size_bytes, 0) "
                        "FROM observation_frames AS f "
                        "JOIN observation_records AS r ON r.feedback_id=f.feedback_id "
                        "WHERE r.session_id=? AND f.storage_path IS NOT NULL",
                        (sid,),
                    ).fetchall()
                    self._db.execute(
                        "DELETE FROM observation_frames WHERE feedback_id IN "
                        "(SELECT feedback_id FROM observation_records WHERE session_id=?)",
                        (sid,),
                    )
                    self._db.execute(
                        "DELETE FROM observation_records WHERE session_id=?", (sid,)
                    )
                    self._db.execute(
                        "DELETE FROM observation_sequence_reservations WHERE session_id=?",
                        (sid,),
                    )
                else:
                    normalized_epoch = int(epoch)
                    rows = self._db.execute(
                        "SELECT f.storage_path, COALESCE(f.size_bytes, 0) "
                        "FROM observation_frames AS f "
                        "JOIN observation_records AS r ON r.feedback_id=f.feedback_id "
                        "WHERE r.session_id=? AND r.epoch=? "
                        "AND f.storage_path IS NOT NULL",
                        (sid, normalized_epoch),
                    ).fetchall()
                    self._db.execute(
                        "DELETE FROM observation_frames WHERE feedback_id IN "
                        "(SELECT feedback_id FROM observation_records "
                        "WHERE session_id=? AND epoch=?)",
                        (sid, normalized_epoch),
                    )
                    self._db.execute(
                        "DELETE FROM observation_records WHERE session_id=? AND epoch=?",
                        (sid, normalized_epoch),
                    )
                    self._db.execute(
                        "DELETE FROM observation_sequence_reservations "
                        "WHERE session_id=? AND epoch=?",
                        (sid, normalized_epoch),
                    )
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
        return [(Path(row[0]), int(row[1] or 0)) for row in rows]

    @staticmethod
    def _decode_feedback(row: tuple[Any, ...]) -> EnvironmentFeedback:
        value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return EnvironmentFeedback.model_validate(value)


def name_for_ref(ref: ObservationRef, refs: Mapping[str, ObservationRef]) -> str:
    for name, candidate in refs.items():
        if candidate.uri == ref.uri:
            return name
    return "frame"


def await_payload_sync(payload: Any) -> bytes:
    """Resolve a pending in-memory payload without requiring an event loop."""

    from .observation_store import payload_to_bytes

    return payload_to_bytes(payload)


__all__ = [
    "OBSERVATION_CACHE_METRIC_NAMES",
    "ObservationCache",
    "ObservationCacheClosedError",
    "ObservationPersistenceDropped",
    "make_observation_uri",
    "parse_observation_uri",
]
