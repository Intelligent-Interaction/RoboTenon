"""JSON-lines service clients and servers over registered stream transports.

The first implementation can run all services in one Python process.  This
module provides the same protocol over a Unix Domain Socket when Environment,
Monitor, Workflow and Body need separate processes on one host.  It deliberately
uses only the Python standard library so a robot-side process does not need the
web or observability dependencies.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from ..bodies.base import BaseBodyService
from ..contracts.serialization import jsonable as _jsonable
from ..contracts.service_context import ServiceContext
from ..schemas import (
    ActionChunk,
    BodyExecutionResult,
    EnvironmentCommand,
    EnvironmentFeedback,
    EnvironmentHealth,
    MonitorReport,
    ObservationRef,
)
from .protocol import BodyService, EnvironmentService, MonitorService
from .transports import build_transport

RPC_VERSION = 1
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def _model(model: Any, value: Any) -> Any:
    validator = getattr(model, "model_validate", None)
    if callable(validator):
        return validator(value)
    return model.parse_obj(value)


class JsonRpcError(RuntimeError):
    """Remote service returned an error response."""


class JsonRpcClient:
    """Persistent, serialized request/response client over a stream transport.

    Calls are serialized per client so one connection remains valid even for
    services that process requests sequentially.  A caller may create two
    clients for the same endpoint when it needs independent channels (for
    example, one for Workflow and one for Monitor).
    """

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        timeout: float = 5.0,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ):
        if max_message_bytes <= 0:
            raise ValueError("IPC max_message_bytes must be positive")
        self.socket_path = str(socket_path)
        self.transport = build_transport(socket_path, transport=transport,
            plugin_transports=plugin_transports, transport_params=transport_params)
        self.timeout = timeout
        self.max_message_bytes = int(max_message_bytes)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._request_id = 0

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        request_timeout = self.timeout if timeout is None else timeout
        if request_timeout <= 0:
            raise ValueError("IPC request timeout must be positive")
        return await asyncio.wait_for(self._call_locked(method, params, timeout=request_timeout), request_timeout)

    async def _call_locked(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        async with self._lock:
            request_timeout = self.timeout if timeout is None else timeout
            if request_timeout <= 0:
                raise ValueError("IPC request timeout must be positive")
            await self._connect(timeout=request_timeout)
            self._request_id += 1
            request = {
                "version": RPC_VERSION,
                "id": self._request_id,
                "method": method,
                "params": _jsonable(params or {}),
            }
            try:
                assert self._writer is not None and self._reader is not None
                encoded = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
                if len(encoded) > self.max_message_bytes:
                    raise ValueError(
                        f"IPC request exceeds max_message_bytes={self.max_message_bytes}"
                    )
                self._writer.write(encoded)
                await asyncio.wait_for(self._writer.drain(), timeout=request_timeout)
                raw = await asyncio.wait_for(self._reader.readline(), timeout=request_timeout)
                if not raw:
                    raise ConnectionError("service closed the IPC connection")
                if len(raw) > self.max_message_bytes:
                    raise ValueError(
                        f"IPC response exceeds max_message_bytes={self.max_message_bytes}"
                    )
                response = json.loads(raw.decode("utf-8"))
            except (Exception, asyncio.CancelledError):
                await self._disconnect()
                raise
            if int(response.get("version", RPC_VERSION)) != RPC_VERSION:
                await self._disconnect()
                raise JsonRpcError("service protocol version does not match client")
            if response.get("id") != request["id"]:
                await self._disconnect()
                raise JsonRpcError("service response id does not match request")
            if "error" in response:
                raise JsonRpcError(str(response["error"]))
            return response.get("result")

    async def close_connection(self) -> None:
        async with self._lock:
            await self._disconnect()
            await self.transport.close()

    async def _connect(self, *, timeout: float | None = None) -> None:
        if self._reader is not None and self._writer is not None and not self._writer.is_closing():
            return
        self._reader, self._writer = await asyncio.wait_for(
            self.transport.connect(limit=self.max_message_bytes),
            timeout=self.timeout if timeout is None else timeout,
        )

    async def _disconnect(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


class JsonRpcServer:
    """JSON-lines server used to expose one resident service process."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        handlers: Mapping[str, Callable[[Mapping[str, Any]], Any | Awaitable[Any]]],
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ) -> None:
        if max_message_bytes <= 0:
            raise ValueError("IPC max_message_bytes must be positive")
        self.socket_path = Path(socket_path)
        self.transport = build_transport(socket_path, transport=transport,
            plugin_transports=plugin_transports, transport_params=transport_params)
        self.handlers = dict(handlers)
        self.max_message_bytes = int(max_message_bytes)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        try:
            self._server = await self.transport.listen(self._handle, limit=self.max_message_bytes)
        except BaseException:
            await self.transport.close()
            raise

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.transport.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                if len(raw) > self.max_message_bytes:
                    raise ValueError(
                        f"IPC request exceeds max_message_bytes={self.max_message_bytes}"
                    )
                request: Any = {}
                try:
                    request = json.loads(raw.decode("utf-8"))
                    if not isinstance(request, Mapping):
                        raise TypeError("IPC request must be a JSON object")
                    version = int(request.get("version", RPC_VERSION))
                    if version != RPC_VERSION:
                        raise ValueError(
                            f"unsupported IPC protocol version {version}; expected {RPC_VERSION}"
                        )
                    method = str(request["method"])
                    handler = self.handlers[method]
                    params = request.get("params") or {}
                    result = handler(params)
                    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                        result = await result
                    response = {
                        "version": RPC_VERSION,
                        "id": request.get("id"),
                        "result": _jsonable(result),
                    }
                except Exception as exc:
                    response = {
                        "version": RPC_VERSION,
                        "id": request.get("id") if isinstance(request, Mapping) else None,
                        "error": str(exc),
                    }
                encoded = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                if len(encoded) > self.max_message_bytes:
                    encoded = (
                        json.dumps(
                            {
                                "version": RPC_VERSION,
                                "id": request.get("id") if isinstance(request, Mapping) else None,
                                "error": "IPC response exceeds configured message limit",
                            }
                        )
                        + "\n"
                    ).encode("utf-8")
                writer.write(encoded)
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


class EnvironmentServiceClient(JsonRpcClient):
    """EnvironmentService implementation backed by ``JsonRpcServer``."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        timeout: float = 5.0,
        observation_pipeline: Any = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ):
        super().__init__(
            socket_path,
            timeout=timeout,
            max_message_bytes=max_message_bytes,
            transport=transport, plugin_transports=plugin_transports, transport_params=transport_params,
        )
        if observation_pipeline is None:
            try:
                from ..infra.observation_runtime import get_observation_pipeline

                observation_pipeline = get_observation_pipeline()
            except Exception:
                observation_pipeline = None
        self.observation_pipeline = observation_pipeline
        self._session_id: Any = None
        self._latest: EnvironmentFeedback | None = None

    async def start(self) -> None:
        await self.call("start")

    async def reset(self, context: Any) -> EnvironmentFeedback:
        value = await self.call("reset", {"context": _jsonable(ServiceContext.from_context(context))})
        return await self._process(_model(EnvironmentFeedback, value), reset=True)

    async def observe(self, request: Any = None) -> EnvironmentFeedback:
        params = {} if request is None else {"request": _jsonable(request)}
        return await self._process(
            _model(EnvironmentFeedback, await self.call("observe", params)),
            reset=False,
        )

    async def command(self, command: EnvironmentCommand) -> EnvironmentFeedback:
        return await self._process(
            _model(EnvironmentFeedback, await self.call("command", {"command": _jsonable(command)})),
            reset=getattr(command, "type", None) == "reset",
        )

    async def health(self) -> EnvironmentHealth:
        return _model(EnvironmentHealth, await self.call("health"))

    async def close(self) -> None:
        try:
            await self.call("close")
        finally:
            if self.observation_pipeline is not None:
                cache = getattr(self.observation_pipeline, "cache", None)
                flush = getattr(cache, "flush", None)
                if callable(flush):
                    result = flush()
                    if inspect.isawaitable(result):
                        await result
            await self.close_connection()

    async def _process(self, value: EnvironmentFeedback, *, reset: bool) -> EnvironmentFeedback:
        if self.observation_pipeline is not None:
            value = await self.observation_pipeline.ingest(
                value,
                raw_payload=value,
                session_id=self._session_id,
                reset=reset,
            )
        self._latest = value
        self._session_id = (value.metadata or {}).get(
            "_cache_session_id", value.session_id
        )
        return value

    @property
    def latest_feedback(self) -> EnvironmentFeedback | None:
        return self._latest

    @property
    def session_id(self) -> Any:
        return self._session_id


class BodyServiceClient(JsonRpcClient, BaseBodyService):
    """BodyService implementation backed by ``JsonRpcServer``."""

    def __init__(
        self,
        socket_path,
        *,
        timeout: float = 5.0,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ):
        super().__init__(
            socket_path,
            timeout=timeout,
            max_message_bytes=max_message_bytes,
            transport=transport, plugin_transports=plugin_transports, transport_params=transport_params,
        )
        # Control traffic must not wait behind a long execute response.
        self.control = JsonRpcClient(
            socket_path,
            timeout=timeout,
            max_message_bytes=max_message_bytes,
            transport=transport, plugin_transports=plugin_transports, transport_params=transport_params,
        )

    async def start(self) -> None:
        await self.call("start")

    async def execute(self, action_chunk: ActionChunk) -> BodyExecutionResult:
        # The transport timeout must cover the declared chunk deadline plus a
        # small response margin.  A fixed five-second RPC timeout could drop
        # a valid long-running chunk while the body keeps moving the robot.
        request_timeout = max(
            self.timeout,
            action_chunk.max_duration_ms / 1000.0 + 1.0,
            max((action.timeout_ms for action in action_chunk.actions), default=0) / 1000.0 + 1.0,
        )
        try:
            value = await self.call(
                "execute",
                {"action_chunk": _jsonable(action_chunk)},
                timeout=request_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError) as exc:
            # If the response channel itself times out, ask the resident Body
            # over a fresh connection to stop.  The original exception is
            # re-raised so Workflow records the transport failure.
            try:
                reason = "body_client_execute_cancelled" if isinstance(exc, asyncio.CancelledError) else "body_client_execute_timeout"
                await self.interrupt(reason)
            except Exception:
                pass
            raise
        return _model(BodyExecutionResult, value)

    async def interrupt(self, reason: str, *, safe_action: ActionChunk | None = None) -> BodyExecutionResult:
        value = await self.control.call("interrupt", {"reason": reason, "safe_action": _jsonable(safe_action)})
        return _model(BodyExecutionResult, value)

    async def health(self) -> EnvironmentHealth:
        return _model(EnvironmentHealth, await self.control.call("health"))

    async def close_connection(self) -> None:
        await self.control.close_connection()
        await super().close_connection()

    async def close(self) -> None:
        try:
            await self.control.call("close")
        finally:
            await self.close_connection()


class MonitorServiceClient(JsonRpcClient):
    """MonitorService implementation backed by ``JsonRpcServer``."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        timeout: float = 5.0,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ):
        super().__init__(
            socket_path,
            timeout=timeout,
            max_message_bytes=max_message_bytes,
            transport=transport, plugin_transports=plugin_transports, transport_params=transport_params,
        )

    async def start(self) -> None:
        await self.call("start")

    async def check(self, feedback: EnvironmentFeedback | None = None, *, context: Any | None = None) -> MonitorReport:
        value = await self.call("check", {"feedback": _jsonable(feedback),
            "context": _jsonable(ServiceContext.from_context(context)) if context is not None else None})
        return _model(MonitorReport, value)

    async def request(
        self,
        name: str | None = None,
        request: Mapping[str, Any] | None = None,
        *,
        feedback: EnvironmentFeedback | None = None,
        context: Any | None = None,
    ) -> MonitorReport:
        """Invoke one/all request-capable monitors in the resident process."""

        value = await self.call(
            "request",
            {
                "name": name,
                "request": _jsonable(dict(request or {})),
                "feedback": _jsonable(feedback),
                "context": (
                    _jsonable(ServiceContext.from_context(context))
                    if context is not None
                    else None
                ),
            },
        )
        return _model(MonitorReport, value)

    async def latest(self) -> MonitorReport | None:
        value = await self.call("latest")
        return None if value is None else _model(MonitorReport, value)

    async def health(self) -> EnvironmentHealth:
        return _model(EnvironmentHealth, await self.call("health"))

    async def close(self) -> None:
        try:
            await self.call("close")
        finally:
            await self.close_connection()


class EnvironmentServiceServer(JsonRpcServer):
    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        service: EnvironmentService,
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ):
        if getattr(service, "observation_pipeline", None) is None:
            try:
                from ..infra.observation_runtime import get_observation_pipeline
                from .environment import ObservationEnvironmentService

                pipeline = get_observation_pipeline()
                if pipeline is not None:
                    service = ObservationEnvironmentService(service, pipeline)
            except Exception:
                pass

        async def reset(params):
            raw = params.get("context")
            context = ServiceContext.from_context(raw) if isinstance(raw, Mapping) else raw
            return await service.reset(context)

        async def start(params):
            method = getattr(service, "start", None)
            if callable(method):
                result = method()
                if inspect.isawaitable(result):
                    return await result
            return None

        async def observe(params):
            method = getattr(service, "observe", None)
            if not callable(method):
                raise AttributeError("environment service does not implement observe()")
            request = params.get("request")
            from .environment import invoke_observe

            result = invoke_observe(method, request)
            return await result if inspect.isawaitable(result) else result

        async def command(params):
            return await service.command(_model(EnvironmentCommand, params["command"]))

        super().__init__(
            socket_path,
            {
                "start": start,
                "reset": reset,
                "observe": observe,
                "command": command,
                "health": lambda params: service.health(),
                "close": lambda params: service.close(),
            },
            max_message_bytes=max_message_bytes,
            transport=transport, plugin_transports=plugin_transports, transport_params=transport_params,
        )


class BodyServiceServer(JsonRpcServer):
    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        service: BodyService,
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ):
        async def execute(params):
            return await service.execute(_model(ActionChunk, params["action_chunk"]))

        async def interrupt(params):
            raw = params.get("safe_action")
            safe_action = _model(ActionChunk, raw) if raw else None
            return await service.interrupt(str(params.get("reason", "remote_interrupt")), safe_action=safe_action)

        super().__init__(
            socket_path,
            {
                "start": lambda params: service.start(),
                "execute": execute,
                "interrupt": interrupt,
                "health": lambda params: service.health(),
                "close": lambda params: service.close(),
            },
            max_message_bytes=max_message_bytes,
            transport=transport, plugin_transports=plugin_transports, transport_params=transport_params,
        )


class MonitorServiceServer(JsonRpcServer):
    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        service: MonitorService,
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ):
        async def check(params):
            raw_feedback = params.get("feedback")
            raw_context = params.get("context")
            feedback = _model(EnvironmentFeedback, raw_feedback) if raw_feedback else None
            context = ServiceContext.from_context(raw_context) if isinstance(raw_context, Mapping) else raw_context
            return await service.check(feedback, context=context)

        async def latest(params):
            return await service.latest()

        async def request(params):
            method = getattr(service, "request", None)
            if not callable(method):
                raise RuntimeError(
                    "monitor service does not implement request()"
                )
            raw_feedback = params.get("feedback")
            raw_context = params.get("context")
            feedback = _model(EnvironmentFeedback, raw_feedback) if raw_feedback else None
            context = ServiceContext.from_context(raw_context) if isinstance(raw_context, Mapping) else raw_context
            payload = params.get("request") or {}
            if not isinstance(payload, Mapping):
                raise TypeError("monitor request payload must be an object")
            result = method(
                params.get("name"),
                dict(payload),
                feedback=feedback,
                context=context,
            )
            return await result if inspect.isawaitable(result) else result

        super().__init__(
            socket_path,
            {
                "start": lambda params: service.start(),
                "check": check,
                "request": request,
                "latest": latest,
                "health": lambda params: service.health(),
                "close": lambda params: service.close(),
            },
            max_message_bytes=max_message_bytes,
            transport=transport, plugin_transports=plugin_transports, transport_params=transport_params,
        )


class ObservationCacheServiceClient(JsonRpcClient):
    """Metadata/query client for a resident observation-cache daemon.

    RPC messages intentionally contain feedback metadata and logical URIs
    only.  Media bytes are resolved by a local ``ObservationResolver`` using
    the shared filesystem/object store configured for the caller process.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        timeout: float = 5.0,
        shared_root: str | os.PathLike[str] | None = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ):
        super().__init__(
            socket_path,
            timeout=timeout,
            max_message_bytes=max_message_bytes,
            transport=transport, plugin_transports=plugin_transports, transport_params=transport_params,
        )
        self.shared_root = Path(shared_root) if shared_root is not None else None
        self.uri_scheme = "cache"
        self._local_paths: dict[str, Path] = {}

    async def start(self) -> None:
        """Match the in-process cache lifecycle (the daemon is already resident)."""

        return

    async def close(self) -> None:
        """Close this process's RPC connection; keep the daemon resident."""

        await self.close_connection()

    async def flush(self, timeout: float | None = None) -> None:
        """Wait until the resident daemon has drained its persistence queue."""

        await self.call("flush", {"timeout": timeout})

    async def stats(self) -> Mapping[str, Any]:
        """Return resident L0/L1 queue statistics from the daemon."""

        value = await self.call("stats")
        return value if isinstance(value, Mapping) else {}

    def make_ref(self, **kwargs: Any) -> ObservationRef:
        """Create a URI ref using exactly the in-process cache contract."""

        from ..infra.observation_cache import ObservationCache

        return ObservationCache.make_ref(self, **kwargs)

    def _sequence(self, feedback: EnvironmentFeedback) -> int:
        from ..infra.observation_cache import ObservationCache

        return ObservationCache._sequence(feedback)

    async def append(
        self,
        feedback: Any,
        payloads: Mapping[str, Any] | None = None,
        *,
        durable: bool = False,
    ) -> EnvironmentFeedback:
        """Append URI-only metadata to the daemon.

        Raw media is never placed in JSON-RPC.  When ``shared_root`` is
        configured, the producer materialises each payload at the same
        deterministic path the daemon uses, then sends only the resulting
        reference/checksum metadata.  Without a shared store, callers must
        provide refs that were materialised by another transport (HTTP,
        object storage, mmap, ...).
        """

        from ..infra.observation_cache import ObservationCache, _logical_session_id
        from ..infra.observation_store import FileStore

        raw_frames = feedback.get("frames") if isinstance(feedback, Mapping) else None
        canonical = ObservationCache._feedback_from(feedback)
        source = payloads if payloads is not None else raw_frames
        canonical, payload_map = ObservationCache._frame_payloads(self, canonical, source)
        canonical = ObservationCache._sanitize_feedback(canonical)
        if payload_map:
            if self.shared_root is None:
                raise ValueError(
                    "remote observation append cannot transport raw payloads; "
                    "configure shared_root or send cache:///object-store refs"
                )
            store = FileStore(self.shared_root)
            sid = _logical_session_id(canonical)
            sequence = self._sequence(canonical)
            updates: dict[str, ObservationRef] = dict(canonical.frames)
            for name, payload in payload_map.items():
                ref = updates.get(name)
                if ref is None:
                    continue
                extension = str(ref.metadata.get("extension", ".bin"))
                path = store.path_for(
                    session_id=sid,
                    epoch=canonical.epoch,
                    sequence=sequence,
                    frame_name=name,
                    extension=extension,
                )
                stored = await asyncio.to_thread(
                    store.put,
                    payload,
                    path,
                    media_type=ref.media_type,
                    encoding=ref.encoding,
                    shape=ref.shape,
                    dtype=ref.dtype,
                )
                updates[name] = ref.model_copy(
                    update={"sha256": stored.sha256, "size_bytes": stored.size_bytes}
                )
                self._local_paths[ref.uri] = path
            canonical = canonical.model_copy(update={"frames": updates})
        value = await self.call(
            "append",
            {"feedback": _jsonable(canonical), "durable": durable},
        )
        return _model(EnvironmentFeedback, value)

    async def latest(self, session_id: Any, epoch: int | None = None) -> EnvironmentFeedback | None:
        value = await self.call("latest", {"session_id": str(session_id), "epoch": epoch})
        return None if value is None else _model(EnvironmentFeedback, value)

    async def history(self, session_id: Any, epoch: int | None = None, *, length: int = 4, interval: int = 1) -> list[EnvironmentFeedback]:
        value = await self.call("history", {"session_id": str(session_id), "epoch": epoch, "length": length, "interval": interval})
        return [_model(EnvironmentFeedback, item) for item in (value or [])]

    async def get_by_feedback_id(self, feedback_id: Any) -> EnvironmentFeedback | None:
        value = await self.call("get_by_feedback_id", {"feedback_id": str(feedback_id)})
        return None if value is None else _model(EnvironmentFeedback, value)

    async def get_by_sequence(self, session_id: Any, sequence: int, *, epoch: int | None = None) -> EnvironmentFeedback | None:
        value = await self.call("get_by_sequence", {"session_id": str(session_id), "sequence": sequence, "epoch": epoch})
        return None if value is None else _model(EnvironmentFeedback, value)

    async def query(self, session_id: Any, *, epoch: int | None = None, sequence: int | None = None, limit: int = 100) -> list[EnvironmentFeedback]:
        value = await self.call("query", {"session_id": str(session_id), "epoch": epoch, "sequence": sequence, "limit": limit})
        return [_model(EnvironmentFeedback, item) for item in (value or [])]

    async def resolve_metadata(self, uri: str) -> Mapping[str, Any] | None:
        value = await self.call("resolve_metadata", {"uri": uri})
        return value if isinstance(value, Mapping) else None

    async def read_uri_bytes(self, uri: str) -> tuple[bytes, dict[str, Any]]:
        metadata = await self.resolve_metadata(uri)
        if not metadata:
            local_path = self._local_paths.get(uri)
            if local_path is not None and local_path.exists():
                return await asyncio.to_thread(local_path.read_bytes), {"uri": uri}
            raise FileNotFoundError(uri)
        storage_path = metadata.get("storage_path")
        path = Path(storage_path) if storage_path else None
        if path is not None and not path.exists() and self.shared_root is not None:
            # A daemon may return an absolute path from another process.  The
            # session/epoch/sequence suffix is stable on a shared filesystem,
            # so remap it under this client's configured root when possible.
            parts = list(path.parts)
            epoch_index = next(
                (index for index, value in enumerate(parts) if value.startswith("epoch-")),
                None,
            )
            if epoch_index is not None and epoch_index > 0:
                path = self.shared_root.joinpath(*parts[epoch_index - 1 :])
        if path is None or not path.exists():
            raise FileNotFoundError(uri)
        return await asyncio.to_thread(path.read_bytes), dict(metadata)

    async def resolve(self, uri: str) -> Any:
        from ..infra.observation_resolver import ObservationResolver

        return await ObservationResolver(self).resolve(uri)

    async def resolve_ref(self, ref: Any) -> Any:
        from ..infra.observation_resolver import ObservationResolver

        return await ObservationResolver(self).resolve_ref(ref)

    async def resolve_feedback(self, feedback: EnvironmentFeedback) -> Any:
        from ..infra.observation_resolver import ObservationResolver

        return await ObservationResolver(self).resolve_feedback(feedback)

    async def clear(self, session_id: Any, epoch: int | None = None) -> None:
        await self.call("clear", {"session_id": str(session_id), "epoch": epoch})


class ObservationCacheServiceServer(JsonRpcServer):
    """Expose cache metadata operations over the existing JSON-lines UDS."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        cache: Any,
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        transport: str = "uds",
        plugin_transports: tuple[str, ...] = (),
        transport_params: Mapping[str, Any] | None = None,
    ):
        async def append(params):
            return await cache.append(
                _model(EnvironmentFeedback, params["feedback"]),
                durable=bool(params.get("durable", False)),
            )

        async def latest(params):
            return await cache.latest(params.get("session_id"), params.get("epoch"))

        async def history(params):
            return await cache.history(
                params.get("session_id"),
                params.get("epoch"),
                length=int(params.get("length", 4)),
                interval=int(params.get("interval", 1)),
            )

        async def get_by_feedback_id(params):
            return await cache.get_by_feedback_id(params.get("feedback_id"))

        async def resolve_metadata(params):
            query = getattr(cache, "_query_frame", None)
            if not callable(query):
                return None
            return await asyncio.to_thread(query, str(params.get("uri", "")))

        async def get_by_sequence(params):
            return await cache.get_by_sequence(
                params.get("session_id"),
                int(params.get("sequence", 0)),
                epoch=params.get("epoch"),
            )

        async def query(params):
            return await cache.query(
                params.get("session_id"),
                epoch=params.get("epoch"),
                sequence=params.get("sequence"),
                limit=int(params.get("limit", 100)),
            )

        async def clear(params):
            return await cache.clear(params.get("session_id"), params.get("epoch"))

        async def flush(params):
            return await cache.flush(params.get("timeout"))

        async def stats(params):
            return await cache.stats()

        super().__init__(
            socket_path,
            {
                "append": append,
                "latest": latest,
                "history": history,
                "get_by_feedback_id": get_by_feedback_id,
                "resolve_metadata": resolve_metadata,
                "get_by_sequence": get_by_sequence,
                "query": query,
                "clear": clear,
                "flush": flush,
                "stats": stats,
            },
            max_message_bytes=max_message_bytes,
            transport=transport, plugin_transports=plugin_transports, transport_params=transport_params,
        )


# Short names are convenient when a deployment has a dedicated cache process.
ObservationCacheClient = ObservationCacheServiceClient
ObservationCacheServer = ObservationCacheServiceServer


__all__ = [
    "BodyServiceClient",
    "BodyServiceServer",
    "EnvironmentServiceClient",
    "EnvironmentServiceServer",
    "JsonRpcClient",
    "JsonRpcError",
    "JsonRpcServer",
    "MonitorServiceClient",
    "MonitorServiceServer",
    "ObservationCacheClient",
    "ObservationCacheServer",
    "ObservationCacheServiceClient",
    "ObservationCacheServiceServer",
]
