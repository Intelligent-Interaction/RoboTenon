"""Resident/request monitor runtime and safety publication."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator, Mapping
from typing import Any

from ..envs.runtime import EnvironmentNotBoundError, bind_environment, environment
from ..runtime.safety import (
    GLOBAL_SAFETY_STATE,
    SafetyStateStore,
    async_reconcile_safety_snapshot,
    async_safety_call,
)
from ..schemas import (
    EnvironmentFeedback,
    EnvironmentHealth,
    MonitorReport,
    SafetyLevel,
)
from .base import MonitorContext, MonitorMode

_UNSET = object()


class MonitorRuntime(Mapping[str, Any]):
    """Manage configured monitor instances.

    Each instance can be configured with ``resident``, ``request`` or both
    modes. Resident mode owns a periodic observation task; request mode is
    invoked through :meth:`request` (and never starts a polling task). The
    runtime publishes every returned report to the shared SafetyStateStore, so
    a P3/P4 result follows the normal Body watchdog and Workflow emergency
    graph path.
    """

    # Local reports are published here; Workflow only needs to apply reports
    # from a remote client that does not own this SafetyStateStore.
    publishes_safety = True

    def __init__(
        self,
        instances: Mapping[str, Any],
        *,
        modes: Mapping[str, set[MonitorMode] | frozenset[MonitorMode]],
        aliases: Mapping[str, str] | None = None,
        metadata: Mapping[str, Mapping[str, Any]] | None = None,
        environment_service: Any = None,
        observation_resolver: Any = None,
        safety_state: SafetyStateStore = GLOBAL_SAFETY_STATE,
        params: Any = None,
        poll_hz: float = 20.0,
        poll_hz_by_id: Mapping[str, float | None] | None = None,
        on_error_level: SafetyLevel = SafetyLevel.P3,
        on_error_level_by_id: Mapping[str, SafetyLevel] | None = None,
    ) -> None:
        if poll_hz <= 0:
            raise ValueError("poll_hz must be positive")
        self._instances = dict(instances)
        self._modes = {
            key: frozenset(value)
            for key, value in modes.items()
            if key in self._instances
        }
        invalid_modes = {
            mode
            for values in self._modes.values()
            for mode in values
            if mode not in {"resident", "request"}
        }
        if invalid_modes:
            raise ValueError(
                "unsupported monitor mode(s): " + ", ".join(sorted(invalid_modes))
            )
        missing = set(self._instances) - set(self._modes)
        if missing:
            raise ValueError("monitor mode is missing for: " + ", ".join(sorted(missing)))
        if any(not value for value in self._modes.values()):
            raise ValueError("each monitor must enable resident and/or request mode")
        self._aliases = dict(aliases or {})
        self._metadata = {
            key: dict(value) for key, value in (metadata or {}).items()
        }
        self._order = tuple(self._instances)
        if environment_service is not None and getattr(
            environment_service, "observation_pipeline", None
        ) is None:
            try:
                from ..infra.observation_runtime import get_observation_pipeline
                from ..services.environment import ObservationEnvironmentService

                pipeline = get_observation_pipeline()
                if pipeline is not None:
                    environment_service = ObservationEnvironmentService(
                        environment_service, pipeline
                    )
            except Exception:
                pass
        self._environment_service = environment_service
        if observation_resolver is None:
            try:
                from ..infra.observation_runtime import get_observation_resolver

                observation_resolver = get_observation_resolver()
            except Exception:
                observation_resolver = None
        self.observation_resolver = observation_resolver
        self.safety_state = safety_state
        self.params = params
        self.poll_hz = poll_hz
        self._poll_hz_by_id = dict(poll_hz_by_id or {})
        self.on_error_level = self._coerce_level(on_error_level)
        self._on_error_level_by_id = {
            key: self._coerce_level(value)
            for key, value in (on_error_level_by_id or {}).items()
        }
        self._started = False
        self._stopping = False
        self._lifecycle_lock = asyncio.Lock()
        self._resident_tasks: dict[str, asyncio.Task[None]] = {}
        self._latest: dict[str, MonitorReport] = {}
        self._errors: dict[str, str] = {}
        self._error_active: set[str] = set()
        self._started_instances: list[Any] = []
        self._global_binding_previous: Any = _UNSET
        # Keep the exact object installed in the process singleton.  A bare
        # environment may be decorated with ObservationEnvironmentService by
        # ``bind_environment``; comparing only against the caller-supplied
        # object would then leave a wrapper behind after monitor shutdown.
        self._global_binding_current: Any = _UNSET
        # A monitor configured in both modes deliberately shares one instance.
        # Serialize calls per instance so a periodic model evaluation cannot
        # race a workflow request and corrupt stateful caches.
        self._invoke_locks: dict[str, asyncio.Lock] = {
            key: asyncio.Lock() for key in self._instances
        }

    @staticmethod
    def _coerce_level(value: SafetyLevel | str) -> SafetyLevel:
        return value if isinstance(value, SafetyLevel) else SafetyLevel(str(value).upper())

    def __getitem__(self, key: str) -> Any:
        return self.resolve(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._order)

    def __len__(self) -> int:
        return len(self._instances)

    def resolve(self, name: str) -> Any:
        key = name if name in self._instances else self._aliases.get(name)
        if key is None or key not in self._instances:
            available = ", ".join(self.names()) or "<none>"
            raise KeyError(f"unknown monitor {name!r}; available: {available}")
        return self._instances[key]

    def resolve_id(self, name: str) -> str:
        if name in self._instances:
            return name
        key = self._aliases.get(name)
        if key is None or key not in self._instances:
            available = ", ".join(self.names()) or "<none>"
            raise KeyError(f"unknown monitor {name!r}; available: {available}")
        return key

    def names(self) -> list[str]:
        return list(self._order)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def environment_service(self) -> Any:
        return self._resolve_environment(required=False)

    def bind_environment(self, service: Any) -> Any:
        """Set the client used by resident monitors and return the old client."""

        if service is None:
            raise ValueError("monitor environment service cannot be None")
        previous = self._environment_service
        self._environment_service = service
        if self._started:
            self._ensure_resident_tasks()
        return previous

    def set_observation_resolver(self, resolver: Any) -> None:
        """Attach the shared URI resolver without rebuilding monitors."""

        self.observation_resolver = resolver

    def _resolve_environment(self, *, required: bool) -> Any:
        service = self._environment_service
        if service is None or service is environment:
            service = environment.current
        if required and service is None:
            raise EnvironmentNotBoundError(
                "resident monitor requires an environment service; bind one with "
                "environment.bind(service) or MonitorRuntime.bind_environment(service)"
            )
        return service

    def _resident_ids(self) -> list[str]:
        return [
            key for key in self._order if "resident" in self._modes[key]
        ]

    def _request_ids(self) -> list[str]:
        return [
            key for key in self._order if "request" in self._modes[key]
        ]

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                self._ensure_resident_tasks()
                return
            resident_ids = self._resident_ids()
            if resident_ids:
                self._resolve_environment(required=True)
            self._bind_global_environment_if_needed()
            started: list[Any] = []
            try:
                for key in self._order:
                    monitor = self._instances[key]
                    hook = getattr(monitor, "start", None)
                    if callable(hook):
                        result = hook()
                        if inspect.isawaitable(result):
                            await result
                    started.append(monitor)
                self._started = True
                self._stopping = False
                self._started_instances = started
                self._ensure_resident_tasks()
            except BaseException:
                for monitor in reversed(started):
                    hook = getattr(monitor, "close", None)
                    if callable(hook):
                        try:
                            result = hook()
                            if inspect.isawaitable(result):
                                await result
                        except Exception:
                            pass
                self._restore_global_environment()
                raise

    def _ensure_resident_tasks(self) -> None:
        if not self._started or self._stopping:
            return
        # ``start`` validates this.  A later bind can also activate a runtime
        # that was built before the environment service became available.
        service = self._resolve_environment(required=False)
        if service is None:
            return
        for key in self._resident_ids():
            task = self._resident_tasks.get(key)
            if task is None or task.done():
                self._resident_tasks[key] = asyncio.create_task(
                    self._resident_loop(key),
                    name=f"robotenon-monitor-{key}",
                )

    def _poll_interval(self, key: str) -> float:
        value = self._poll_hz_by_id.get(key)
        hz = self.poll_hz if value is None else float(value)
        if hz <= 0:
            raise ValueError(f"monitor {key!r} poll_hz must be positive")
        return 1.0 / hz

    def _error_level(self, key: str) -> SafetyLevel:
        return self._on_error_level_by_id.get(key, self.on_error_level)

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._stopping = True
            resident_ids = tuple(self._resident_tasks)
            tasks = list(self._resident_tasks.values())
            self._resident_tasks.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # Only clear heartbeats owned by loops this instance started.
            # Disabled monitors and repeated close calls must not touch peers.
            for key in resident_ids:
                await async_safety_call(self.safety_state, "clear_heartbeat", key)
            first_error: Exception | None = None
            for monitor in reversed(self._started_instances):
                hook = getattr(monitor, "close", None)
                if not callable(hook):
                    continue
                try:
                    result = hook()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            self._started = False
            self._started_instances = []
            self._restore_global_environment()
            if first_error is not None:
                raise first_error

    def _bind_global_environment_if_needed(self) -> None:
        """Make an explicitly supplied service visible through the singleton.

        This is a convenience for a monitor-only process.  If another owner
        already bound a service, it remains authoritative; all components are
        expected to share that one service in a normal application.
        """

        service = self._environment_service
        if service is None or service is environment or environment.current is not None:
            return
        self._global_binding_previous = bind_environment(service)
        self._global_binding_current = environment.current

    def _restore_global_environment(self) -> None:
        if self._global_binding_previous is _UNSET:
            return
        current = self._global_binding_current
        if current is _UNSET:
            current = self._environment_service
        if environment.current is current:
            environment.restore(self._global_binding_previous)
        self._global_binding_previous = _UNSET
        self._global_binding_current = _UNSET

    async def _resident_loop(self, key: str) -> None:
        monitor = self._instances[key]
        interval = self._poll_interval(key)
        await async_safety_call(
            self.safety_state,
            "heartbeat",
            key,
            ttl_s=max(1.0, interval * 3.0),
            failure_level=self._error_level(key),
        )
        while not self._stopping:
            try:
                service = self._resolve_environment(required=True)
                feedback = await self._call_service(service, "observe")
                report = await self._invoke(
                    key,
                    monitor,
                    feedback,
                    request=None,
                    graph_context=None,
                )
                self._latest[key] = report
                await self._publish(key, report)
                self._errors.pop(key, None)
                self._error_active.discard(key)
                await async_safety_call(
                    self.safety_state,
                    "clear",
                    code=f"{key}.error",
                    include_latched=False,
                )
                await async_safety_call(
                    self.safety_state,
                    "heartbeat",
                    key,
                    ttl_s=max(1.0, interval * 3.0),
                    failure_level=self._error_level(key),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors[key] = str(exc)
                level = self._error_level(key)
                if key not in self._error_active:
                    await async_safety_call(
                        self.safety_state,
                        "raise_level",
                        level,
                        code=f"{key}.error",
                        source=key,
                        message=f"monitor_check_failed:{exc}",
                        latched=level.rank >= SafetyLevel.P3.rank,
                    )
                    self._error_active.add(key)
            await asyncio.sleep(interval)

    @staticmethod
    async def _call_service(service: Any, method: str, *args: Any, **kwargs: Any) -> Any:
        target = getattr(service, method, None)
        if not callable(target):
            raise AttributeError(f"environment service does not implement {method}()")
        value = target(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    async def _invoke(
        self,
        key: str,
        monitor: Any,
        feedback: EnvironmentFeedback | None,
        *,
        request: Mapping[str, Any] | None,
        graph_context: Any,
    ) -> MonitorReport:
        method = getattr(monitor, "check", None)
        if not callable(method):
            method = getattr(monitor, "evaluate", None)
        if not callable(method) and callable(monitor):
            method = monitor
        if not callable(method):
            raise TypeError(
                f"monitor {key!r} must implement check(feedback, request, context), evaluate() or __call__()"
            )
        service = self._resolve_environment(required=False)
        context = MonitorContext(
            # Pass the actual configured client when one was supplied.  When
            # no local client exists, the process singleton remains the
            # canonical object and gives plugins the usual ``environment``
            # access path.
            environment=service if service is not None else environment,
            safety_state=self.safety_state,
            params=self.params,
            graph_context=graph_context,
            monitor_id=key,
            request=request,
        )
        resolver = self.observation_resolver
        if resolver is None:
            try:
                from ..infra.observation_runtime import get_observation_resolver

                resolver = get_observation_resolver()
            except Exception:
                resolver = None
        if resolver is not None and feedback is not None:
            resolve_feedback = getattr(resolver, "resolve_feedback", None)
            if callable(resolve_feedback):
                view = resolve_feedback(feedback)
                if inspect.isawaitable(view):
                    view = await view
                context.observation_view = view
        async with self._invoke_locks[key]:
            value = self._call_monitor_method(method, feedback, request, context)
            if inspect.isawaitable(value):
                value = await value
        return self._coerce_report(value, feedback, key)

    @staticmethod
    def _call_monitor_method(
        method: Any,
        feedback: EnvironmentFeedback | None,
        request: Mapping[str, Any] | None,
        context: MonitorContext,
    ) -> Any:
        """Call canonical plugins plus the small legacy signature variants."""

        try:
            parameters = list(inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            return method(feedback, request=request, context=context)
        accepts_kwargs = any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters
        )
        kwargs: dict[str, Any] = {}
        names = {item.name for item in parameters}
        if accepts_kwargs or "request" in names:
            kwargs["request"] = request
        if accepts_kwargs or "context" in names:
            kwargs["context"] = context
        positional = [
            item
            for item in parameters
            if item.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if positional:
            first = positional[0].name.lower()
            # A few early plugins named the sole argument ``ctx`` and expect
            # the ephemeral MonitorContext rather than EnvironmentFeedback.
            argument = context if first in {"ctx", "monitor_context"} else feedback
            return method(argument, **kwargs)
        if accepts_kwargs or "feedback" in names:
            kwargs["feedback"] = feedback
        return method(**kwargs)

    @staticmethod
    def _coerce_report(
        value: Any,
        feedback: EnvironmentFeedback | None,
        key: str,
    ) -> MonitorReport:
        if isinstance(value, MonitorReport):
            return value
        session_id = feedback.session_id if feedback is not None else None
        if isinstance(value, SafetyLevel):
            return MonitorReport(
                session_id=session_id,
                compliant=value.rank < SafetyLevel.P3.rank,
                safety_level=value,
                reason=f"monitor_level_{value.value}",
                source=key,
            )
        if isinstance(value, Mapping):
            return MonitorReport.model_validate(
                {"session_id": session_id, "source": key, **dict(value)}
            )
        if isinstance(value, bool):
            return MonitorReport(
                session_id=session_id,
                compliant=value,
                safety_level=SafetyLevel.P0 if value else SafetyLevel.P3,
                reason="ok" if value else "monitor_rejected",
                source=key,
            )
        raise TypeError(
            "monitor must return MonitorReport, SafetyLevel, mapping or bool"
        )

    async def _publish(self, key: str, report: MonitorReport) -> None:
        # A monitor report is a complete snapshot for this owner.  Reconcile
        # stale transient signals instead of appending them forever.  P3/P4
        # signals remain latched until the explicit recovery path clears them.
        await async_reconcile_safety_snapshot(
            self.safety_state,
            key,
            report.safety_level,
            report.signals,
            source=report.source or key,
            reason=report.reason,
            synthetic_code=f"{key}.compliance",
        )

    async def request(
        self,
        name: str | None = None,
        request: Mapping[str, Any] | None = None,
        *,
        feedback: EnvironmentFeedback | None = None,
        context: Any | None = None,
    ) -> MonitorReport:
        """Invoke one or all request-capable monitors on demand.

        A request monitor may also be resident.  In that case this call shares
        the same instance and model state as its periodic loop; it does not
        construct another provider.
        """

        await self.start()
        keys = [self.resolve_id(name)] if name is not None else self._request_ids()
        if not keys:
            latest = await self.latest()
            if latest is not None:
                return latest
            return MonitorReport(
                session_id=feedback.session_id if feedback is not None else None,
                compliant=True,
                safety_level=SafetyLevel.P0,
                reason="no_request_monitor_configured",
                source="monitor-runtime",
            )
        for key in keys:
            if "request" not in self._modes[key]:
                raise ValueError(f"monitor {key!r} is not configured for request mode")
        if feedback is None and self._resolve_environment(required=False) is not None:
            # Fetch one coherent snapshot for request monitors when an
            # environment is available.  Offline/replay callers can still
            # provide feedback explicitly (or leave it as ``None``).
            feedback = await self._call_service(
                self._resolve_environment(required=True), "observe"
            )
        reports: list[MonitorReport] = []
        for key in keys:
            try:
                report = await self._invoke(
                    key,
                    self._instances[key],
                    feedback,
                    request=request,
                    graph_context=context,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors[key] = str(exc)
                level = self._error_level(key)
                if key not in self._error_active:
                    await async_safety_call(
                        self.safety_state,
                        "raise_level",
                        level,
                        code=f"{key}.error",
                        source=key,
                        message=f"monitor_request_failed:{exc}",
                        latched=level.rank >= SafetyLevel.P3.rank,
                    )
                    self._error_active.add(key)
                raise
            self._latest[key] = report
            await self._publish(key, report)
            self._errors.pop(key, None)
            self._error_active.discard(key)
            await async_safety_call(
                self.safety_state,
                "clear",
                code=f"{key}.error",
                include_latched=False,
            )
            reports.append(report)
        # Preserve the provider's source/report identity for the common
        # single-monitor request.  Aggregate only when the caller requested
        # the whole monitor set; this also avoids an unnecessary synthetic
        # ``monitor-runtime.level`` signal in Workflow's remote-report path.
        return reports[0] if len(reports) == 1 else self._aggregate(reports)

    async def check(
        self,
        feedback: EnvironmentFeedback | None = None,
        *,
        context: Any | None = None,
    ) -> MonitorReport:
        """Compatibility check used by the ``monitor.check`` system node."""

        if self._request_ids():
            return await self.request(feedback=feedback, context=context)
        await self.start()
        latest = await self.latest()
        if latest is not None:
            return latest
        keys = self._resident_ids()
        if not keys:
            return MonitorReport(
                session_id=feedback.session_id if feedback is not None else None,
                compliant=True,
                safety_level=SafetyLevel.P0,
                reason="no_monitor_configured",
                source="monitor-runtime",
            )
        if feedback is None and self._resolve_environment(required=False) is not None:
            feedback = await self._call_service(
                self._resolve_environment(required=True), "observe"
            )
        reports: list[MonitorReport] = []
        for key in keys:
            report = await self._invoke(
                key,
                self._instances[key],
                feedback,
                request=None,
                graph_context=context,
            )
            self._latest[key] = report
            await self._publish(key, report)
            reports.append(report)
        return reports[0] if len(reports) == 1 else self._aggregate(reports)

    async def latest(self, name: str | None = None) -> MonitorReport | None:
        if name is not None:
            return self._latest.get(self.resolve_id(name))
        if not self._latest:
            return None
        reports = list(self._latest.values())
        return reports[0] if len(reports) == 1 else self._aggregate(reports)

    @staticmethod
    def _aggregate(reports: list[MonitorReport]) -> MonitorReport:
        if not reports:
            return MonitorReport(
                session_id=None,
                compliant=True,
                safety_level=SafetyLevel.P0,
                reason="no_monitor_report",
                source="monitor-runtime",
            )
        level = max((item.safety_level for item in reports), key=lambda item: item.rank)
        session_ids = {item.session_id for item in reports if item.session_id is not None}
        reasons = [item.reason for item in reports if item.reason]
        return MonitorReport(
            session_id=next(iter(session_ids), None) if len(session_ids) <= 1 else None,
            compliant=all(item.compliant for item in reports) and level.rank < SafetyLevel.P3.rank,
            safety_level=level,
            signals=[signal for item in reports for signal in item.signals],
            reason="; ".join(reasons) if reasons else None,
            source="monitor-runtime",
            metadata={"monitors": [item.source for item in reports]},
        )

    async def health(self) -> EnvironmentHealth:
        resident = self._resident_ids()
        ready = self._started and all(
            key in self._resident_tasks and not self._resident_tasks[key].done()
            for key in resident
        )
        return EnvironmentHealth(
            ready=ready,
            degraded=bool(self._errors),
            message="; ".join(f"{key}: {error}" for key, error in self._errors.items()) or None,
        )

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": key,
                "modes": sorted(self._modes[key]),
                **self._metadata.get(key, {}),
                "started": self._started,
                "resident_running": key in self._resident_tasks
                and not self._resident_tasks[key].done(),
                "last_error": self._errors.get(key),
            }
            for key in self._order
        ]


__all__ = ["MonitorRuntime"]
