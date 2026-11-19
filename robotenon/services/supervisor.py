"""Lifecycle and liveness supervision for the resident RoboTenon services."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..runtime.safety import GLOBAL_SAFETY_STATE, SafetyStateStore, async_safety_call
from ..schemas import EnvironmentHealth, SafetyLevel


@dataclass(slots=True)
class ServiceStatus:
    name: str
    ready: bool = False
    degraded: bool = False
    errors: int = 0
    last_error: str | None = None
    last_ok_at: datetime | None = None


class ServiceSupervisor:
    """Start, health-check and stop Env/Body/Monitor service objects.

    The supervisor is intentionally transport agnostic.  It can supervise
    in-process implementations or UDS clients.  Each successful health check
    renews a safety heartbeat; if this supervisor or one of its services dies,
    the shared ``SafetyStateStore`` exposes an expired P3/P4 signal to Body and
    Workflow on their next poll.
    """

    def __init__(
        self,
        services: Mapping[str, Any],
        *,
        safety_state: SafetyStateStore = GLOBAL_SAFETY_STATE,
        poll_hz: float = 10.0,
        failure_level: SafetyLevel = SafetyLevel.P3,
        heartbeat_ttl_s: float | None = None,
        startup_timeout_s: float = 10.0,
        health_timeout_s: float = 2.0,
    ) -> None:
        if poll_hz <= 0 or startup_timeout_s <= 0 or health_timeout_s <= 0:
            raise ValueError("poll_hz/startup_timeout_s/health_timeout_s must be positive")
        if not services:
            raise ValueError("at least one service is required")
        self.services = dict(services)
        self.safety_state = safety_state
        self.poll_interval = 1.0 / poll_hz
        self.failure_level = failure_level if isinstance(failure_level, SafetyLevel) else SafetyLevel(str(failure_level).upper())
        self.heartbeat_ttl_s = heartbeat_ttl_s or max(1.0, self.poll_interval * 3.0)
        self.startup_timeout_s = startup_timeout_s
        self.health_timeout_s = health_timeout_s
        self.statuses = {name: ServiceStatus(name=name) for name in self.services}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        startup_errors: list[tuple[str, Exception]] = []
        for name, service in self.services.items():
            start = getattr(service, "start", None)
            if callable(start):
                try:
                    result = start()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=self.startup_timeout_s)
                except Exception as exc:
                    startup_errors.append((name, exc))
                    await self._record_failure(name, exc)
        statuses = await self.check_once()
        # Keep startup failures visible even if a partially initialized
        # implementation happens to answer a later health probe as ready.
        failed_health = [status for status in statuses.values() if not status.ready]
        if startup_errors or failed_health:
            for name, exc in startup_errors:
                await self._record_failure(name, exc)
            details = "; ".join(
                [*(f"{name}: {exc}" for name, exc in startup_errors),
                 *(f"{status.name}: {status.last_error or 'not ready'}" for status in failed_health)]
            )
            try:
                await self.close()
            except Exception as cleanup_error:
                details += f"; cleanup: {cleanup_error}"
            raise RuntimeError(f"service supervisor startup failed: {details}")
        self._task = asyncio.create_task(self._run(), name="robotenon-service-supervisor")

    async def check_once(self) -> dict[str, ServiceStatus]:
        """Probe every service once and update safety/liveness state."""
        for name, service in self.services.items():
            status = self.statuses[name]
            try:
                health_method = getattr(service, "health", None)
                if not callable(health_method):
                    raise RuntimeError(f"service {name!r} does not implement health()")
                health = health_method()
                if inspect.isawaitable(health):
                    health = await asyncio.wait_for(health, timeout=self.health_timeout_s)
                if isinstance(health, Mapping):
                    health = EnvironmentHealth.model_validate(health)
                if not isinstance(health, EnvironmentHealth):
                    raise TypeError(f"service {name!r} health() must return EnvironmentHealth")
                status.ready = bool(health.ready)
                status.degraded = bool(health.degraded)
                if not status.ready:
                    raise RuntimeError(health.message or "service is not ready")
                status.last_error = None
                status.last_ok_at = datetime.now(timezone.utc)
                await async_safety_call(
                    self.safety_state,
                    "heartbeat",
                    f"service.{name}",
                    ttl_s=self.heartbeat_ttl_s,
                    failure_level=self.failure_level,
                )
                await async_safety_call(
                    self.safety_state,
                    "clear",
                    code=f"service.{name}.error",
                    include_latched=False,
                )
            except Exception as exc:
                if isinstance(exc, asyncio.TimeoutError):
                    exc = RuntimeError(
                        f"health check timed out after {self.health_timeout_s:.3f}s"
                    )
                await self._record_failure(name, exc)
        return dict(self.statuses)

    async def _record_failure(self, name: str, error: Exception) -> None:
        status = self.statuses[name]
        status.ready = False
        status.degraded = True
        status.errors += 1
        status.last_error = str(error)
        await async_safety_call(
            self.safety_state,
            "raise_level",
            self.failure_level,
            code=f"service.{name}.error",
            source="service-supervisor",
            message=str(error),
            metadata={"service": name},
        )

    async def health(self) -> EnvironmentHealth:
        statuses = await self.check_once()
        failed = [status for status in statuses.values() if not status.ready]
        return EnvironmentHealth(
            ready=not failed,
            degraded=any(status.degraded for status in statuses.values()),
            message=("; ".join(f"{item.name}: {item.last_error}" for item in failed) if failed else None),
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
        for name in self.services:
            await async_safety_call(
                self.safety_state, "clear_heartbeat", f"service.{name}"
            )
        first_error = None
        for service in reversed(list(self.services.values())):
            try:
                close = getattr(service, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=self.startup_timeout_s)
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    async def _run(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(self.poll_interval)
                await self.check_once()
        except asyncio.CancelledError:
            raise


__all__ = ["ServiceStatus", "ServiceSupervisor"]
