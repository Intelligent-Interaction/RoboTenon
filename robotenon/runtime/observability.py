"""Compose task-scoped producers and an explicit, deployment-neutral data source."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..infra.metrics import MetricsClient, MetricsDaemon, UnixSocketMetricsSink
from ..infra.trace import TraceClient, TraceDaemon, UnixSocketTraceSink


@dataclass(frozen=True)
class ExecutionIdentity:
    experiment_id: str
    run_id: str
    episode_id: str
    attempt_id: str
    task_id: str
    adapter_id: str = "single"
    worker_id: str = "local"
    gpu_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        experiment_id: str,
        run_id: str | None = None,
        episode_id: str | None = None,
        adapter_id: str = "single",
        worker_id: str = "local",
        gpu_id: str | None = None,
    ):
        return cls(
            experiment_id,
            run_id or str(uuid4()),
            episode_id or str(uuid4()),
            str(uuid4()),
            task_id,
            adapter_id,
            worker_id,
            gpu_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ObservabilitySource:
    trace_db: str | None = None
    trace_wal: str | None = None
    metrics_wal: str | None = None
    trace_socket: str | None = None
    metrics_socket: str | None = None

    def __post_init__(self):
        for key in self.__dataclass_fields__:
            value = getattr(self, key)
            if value is not None:
                object.__setattr__(self, key, str(Path(value).expanduser().resolve()))
        if self.trace_wal and not self.trace_db:
            object.__setattr__(
                self, "trace_db", str(Path(self.trace_wal).with_suffix(".sqlite3"))
            )

    @classmethod
    def from_params(cls, params, *, trace_dir=None, metrics_dir=None):
        values = params.resolve("runtime.observability") or {}
        allowed = set(cls.__dataclass_fields__)
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown observability settings: {sorted(unknown)}")
        defaults = {}
        if trace_dir:
            defaults.update(
                trace_db=str(Path(trace_dir) / "trace.sqlite3"),
                trace_wal=str(Path(trace_dir) / "trace.jsonl"),
            )
        if metrics_dir:
            defaults["metrics_wal"] = str(Path(metrics_dir) / "metrics.jsonl")
        defaults.update(values)
        source = cls(**defaults)
        if source.trace_socket and not source.trace_db:
            raise ValueError("trace_socket requires trace_db for the query data source")
        return source

    def to_dict(self):
        return asdict(self)


class ObservabilityRuntime:
    def __init__(self, source: ObservabilitySource, identity: ExecutionIdentity):
        self.source = source
        self.identity = identity
        self.trace = None
        self.metrics = None
        self._trace_store = None

    async def start(self):
        try:
            if self.source.trace_socket:
                trace_sink = UnixSocketTraceSink(self.source.trace_socket)
            elif self.source.trace_db or self.source.trace_wal:
                self._trace_store = TraceDaemon(
                    self.source.trace_wal, db_path=self.source.trace_db
                )
                trace_sink = self._trace_store
            else:
                trace_sink = None
            if trace_sink is not None:
                self.trace = TraceClient(trace_sink, identity=self.identity.to_dict())
                await self.trace.start()
            if self.source.metrics_socket:
                metrics_sink = UnixSocketMetricsSink(self.source.metrics_socket)
            elif self.source.metrics_wal:
                metrics_sink = MetricsDaemon(self.source.metrics_wal)
            else:
                metrics_sink = None
            if metrics_sink is not None:
                self.metrics = MetricsClient(
                    metrics_sink, identity=self.identity.to_dict()
                )
                await self.metrics.start()
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        try:
            if self.trace is not None:
                await self.trace.close()
        finally:
            try:
                if self.metrics is not None:
                    await self.metrics.close()
            finally:
                if self._trace_store is not None:
                    self._trace_store.close()
                    self._trace_store = None


class SharedObservability:
    """Owned shared writers, composed by the launcher rather than the Web."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.servers = []
        self.trace_store = None
        self.socket_directory = None
        self.source = None

    async def start(self):
        import tempfile

        from ..infra.ingest_server import IngestServer

        self.directory.mkdir(parents=True, exist_ok=True)
        # Unix sockets have a small pathname limit on macOS and Linux.
        self.socket_directory = tempfile.TemporaryDirectory(
            prefix="robotenon-", dir="/tmp"
        )
        sockets = Path(self.socket_directory.name)
        self.source = ObservabilitySource(
            trace_db=str(self.directory / "trace.sqlite3"),
            trace_wal=str(self.directory / "trace.jsonl"),
            metrics_wal=str(self.directory / "metrics.jsonl"),
            trace_socket=str(sockets / "trace.sock"),
            metrics_socket=str(sockets / "metrics.sock"),
        )
        try:
            self.trace_store = TraceDaemon(
                self.source.trace_wal, db_path=self.source.trace_db
            )
            for path, store in (
                (self.source.trace_socket, self.trace_store),
                (self.source.metrics_socket, MetricsDaemon(self.source.metrics_wal)),
            ):
                server = await IngestServer(path, store).start()
                self.servers.append(server)
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        for server in reversed(self.servers):
            await server.close()
        self.servers.clear()
        if self.trace_store is not None:
            self.trace_store.close()
            self.trace_store = None
        if self.socket_directory is not None:
            self.socket_directory.cleanup()
            self.socket_directory = None


class PortalRuntime:
    """Optional launcher-owned Web server; its data layer remains read-only."""

    def __init__(self, source, settings):
        self.source, self.settings = source, settings or {}
        self.server = self.thread = None

    async def start(self):
        if not self.settings.get("enabled", False):
            return self
        if not self.source.trace_db:
            raise ValueError("runtime.web requires an explicit trace_db data source")
        import threading
        import os

        from ..web.query import PortalData
        from ..web.server import build_server

        data = PortalData(
            self.source.metrics_wal, self.source.trace_wal or "", self.source.trace_db
        )
        self.server = build_server(
            self.settings.get("host", "127.0.0.1"),
            int(self.settings.get("port", 8765)),
            data,
            control_token=os.getenv("ROBOTENON_PORTAL_TOKEN"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    async def close(self):
        if self.server is not None:
            import asyncio

            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()
            await asyncio.to_thread(self.thread.join)
            self.server = self.thread = None
