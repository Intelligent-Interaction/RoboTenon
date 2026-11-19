from __future__ import annotations

import asyncio
import inspect
from typing import Any
from uuid import UUID

from ..runtime.safety import (
    GLOBAL_SAFETY_STATE,
    SafetyStateStore,
    async_reconcile_safety_snapshot,
    async_safety_call,
    max_safety_level,
)
from ..schemas import (
    EnvironmentFeedback,
    EnvironmentHealth,
    MonitorReport,
    SafetyLevel,
    SafetySignal,
)
from .protocol import EnvironmentService, MonitorChecker


class SafetyMonitorService:
    """Resident periodic compliance monitor independent from GraphRuntime.

    The monitor has its own EnvironmentService client, so it can observe the
    world even while Workflow is waiting for a model or body action.  Every
    report is published to the process-wide ``SafetyStateStore``; P3/P4
    signals are latched by that store and cannot disappear on a transient
    healthy observation.
    """

    publishes_safety = True

    def __init__(
        self,
        environment: EnvironmentService,
        *,
        checker: MonitorChecker | None = None,
        safety_state: SafetyStateStore = GLOBAL_SAFETY_STATE,
        poll_hz: float = 20.0,
        name: str = "monitor",
        session_id: UUID | None = None,
        on_error_level: SafetyLevel = SafetyLevel.P3,
    ) -> None:
        if poll_hz <= 0:
            raise ValueError("poll_hz must be positive")
        if getattr(environment, "observation_pipeline", None) is None:
            try:
                from ..infra.observation_runtime import get_observation_pipeline
                from .environment import ObservationEnvironmentService

                pipeline = get_observation_pipeline()
                if pipeline is not None:
                    environment = ObservationEnvironmentService(environment, pipeline)
            except Exception:
                pass
        self.environment = environment
        self.checker = checker
        self.safety_state = safety_state
        self.poll_interval = 1.0 / poll_hz
        self.name = name
        self.session_id = session_id
        self.on_error_level = (
            on_error_level
            if isinstance(on_error_level, SafetyLevel)
            else SafetyLevel(str(on_error_level).upper())
        )
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._latest: MonitorReport | None = None
        self.errors = 0
        self._error_active = False
        self._last_error: str | None = None
        self._last_success_at = None
        self._heartbeat_ttl = max(1.0, self.poll_interval * 3.0)
        # The legacy service exposes both a resident loop and an on-demand
        # request endpoint.  They share one checker/model instance, so calls
        # must not overlap even when a request arrives while a poll is in
        # flight.
        self._check_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        await async_safety_call(
            self.safety_state,
            "heartbeat",
            self.name,
            ttl_s=self._heartbeat_ttl,
            failure_level=self.on_error_level,
        )
        self._task = asyncio.create_task(self._run(), name=f"robotenon-{self.name}")

    async def check(
        self,
        feedback: EnvironmentFeedback | None = None,
        *,
        context: Any | None = None,
    ) -> MonitorReport:
        async with self._check_lock:
            return await self._check_unlocked(feedback, context=context)

    async def _check_unlocked(
        self,
        feedback: EnvironmentFeedback | None = None,
        *,
        context: Any | None = None,
    ) -> MonitorReport:
        if feedback is None:
            feedback = await self.environment.observe()
        if not isinstance(feedback, EnvironmentFeedback):
            raise TypeError("monitor environment client must return EnvironmentFeedback")
        if self.checker is None:
            report = self._default_report(feedback)
        else:
            value = self.checker(feedback, context)
            if inspect.isawaitable(value):
                value = await value
            report = self._coerce_report(value, feedback)
        self._latest = report
        await self._publish(report)
        self._error_active = False
        self._last_error = None
        self._last_success_at = report.checked_at
        await async_safety_call(
            self.safety_state,
            "heartbeat",
            self.name,
            ttl_s=self._heartbeat_ttl,
            failure_level=self.on_error_level,
        )
        # A non-latched monitor transport error can recover automatically;
        # latched P3/P4 errors intentionally remain until explicit recovery.
        await async_safety_call(
            self.safety_state,
            "clear",
            code=f"{self.name}.error",
            include_latched=False,
        )
        return report

    async def latest(self) -> MonitorReport | None:
        return self._latest

    async def request(
        self,
        name: str | None = None,
        request: dict[str, Any] | None = None,
        *,
        feedback: EnvironmentFeedback | None = None,
        context: Any | None = None,
    ) -> MonitorReport:
        """Compatibility request endpoint for the legacy single monitor.

        ``SafetyMonitorService`` predates the registry runtime and represents
        one resident checker.  Treating a request as an immediate check keeps
        old deployments usable while new multi-monitor deployments should use
        :class:`robotenon.monitors.MonitorRuntime`.
        """

        return await self.check(feedback, context=context)

    async def health(self) -> EnvironmentHealth:
        ready = not self._stopping and self._task is not None and not self._task.done()
        return EnvironmentHealth(
            ready=ready,
            degraded=self._error_active,
            message=self._last_error,
        )

    async def close(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await async_safety_call(self.safety_state, "clear_heartbeat", self.name)

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self._last_error = str(exc)
                # A monitor that cannot read the environment must not silently
                # leave Workflow/Body in normal mode.  P3 is conservative and
                # latched by SafetyStateStore; deployments may choose P2 for a
                # redundant monitor setup.
                if not self._error_active:
                    await async_safety_call(
                        self.safety_state,
                        "raise_level",
                        self.on_error_level,
                        code=f"{self.name}.error",
                        source=self.name,
                        message=f"monitor_check_failed:{exc}",
                        latched=self.on_error_level.rank >= SafetyLevel.P3.rank,
                    )
                    self._error_active = True
            await asyncio.sleep(self.poll_interval)

    def _default_report(self, feedback: EnvironmentFeedback) -> MonitorReport:
        levels = [feedback.safety_level]
        signals = []
        for signal in feedback.signals:
            if signal.safety_level is not None:
                levels.append(signal.safety_level)
                signals.append(
                    SafetySignal(
                        code=f"{self.name}.{signal.name}",
                        level=signal.safety_level,
                        source=self.name,
                        message=str(signal.value) if signal.value is not None else None,
                        timestamp=signal.timestamp,
                        latched=signal.safety_level.rank >= SafetyLevel.P3.rank,
                        metadata=signal.metadata,
                    )
                )
        level = max_safety_level(*levels)
        if level.rank >= SafetyLevel.P3.rank:
            compliant = False
            reason = f"safety_level_{level.value}"
        elif feedback.error:
            compliant = False
            reason = "environment_feedback_error"
            level = max_safety_level(level, SafetyLevel.P2)
        else:
            compliant = True
            reason = "ok"
        return MonitorReport(
            session_id=feedback.session_id,
            compliant=compliant,
            safety_level=level,
            signals=signals,
            reason=reason,
            source=self.name,
            metadata={"sequence_no": feedback.sequence_no},
        )

    def _coerce_report(self, value: Any, feedback: EnvironmentFeedback) -> MonitorReport:
        if isinstance(value, MonitorReport):
            return value
        if isinstance(value, SafetyLevel):
            return MonitorReport(
                session_id=feedback.session_id,
                compliant=value.rank < SafetyLevel.P3.rank,
                safety_level=value,
                reason=f"checker_level_{value.value}",
                source=self.name,
            )
        if isinstance(value, dict):
            return MonitorReport.model_validate({"session_id": feedback.session_id, "source": self.name, **value})
        if isinstance(value, bool):
            return MonitorReport(
                session_id=feedback.session_id,
                compliant=value,
                safety_level=SafetyLevel.P0 if value else SafetyLevel.P3,
                reason="ok" if value else "checker_rejected",
                source=self.name,
            )
        raise TypeError("monitor checker must return MonitorReport, SafetyLevel, mapping or bool")

    async def _publish(self, report: MonitorReport) -> None:
        await async_reconcile_safety_snapshot(
            self.safety_state,
            self.name,
            report.safety_level,
            report.signals,
            source=report.source or self.name,
            reason=report.reason,
            synthetic_code=f"{self.name}.compliance",
        )


__all__ = ["SafetyMonitorService"]
