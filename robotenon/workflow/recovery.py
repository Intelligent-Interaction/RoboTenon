"""Background safety-incident recovery orchestration."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from ..runtime.safety import GLOBAL_SAFETY_STATE, SafetyStateStore, async_safety_call
from ..schemas import SafetyLevel

RecoveryControllerStatus = Literal[
    "stopped",
    "running",
    "waiting_manual",
    "recovering",
    "completed",
    "failed",
]


@dataclass(slots=True)
class RecoveryControllerSnapshot:
    status: RecoveryControllerStatus
    incident_id: str | None = None
    level: SafetyLevel = SafetyLevel.P0
    attempts: int = 0
    last_error: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    result: Any | None = None


class RecoveryController:
    """Watch the shared safety state and execute configured recovery graphs.

    ``GraphRuntime.recover`` remains the deterministic one-shot API.  This
    controller supplies the missing resident supervisor: auto policies are
    invoked without approval, while manual policies pause in
    ``waiting_manual`` until :meth:`approve` is called by a human or hardware
    controller.  Only one recovery attempt runs at a time.
    """

    def __init__(
        self,
        runtime: Any,
        graph_id: str,
        ctx: Any,
        *,
        body: Any = None,
        monitor: Any = None,
        safety_state: SafetyStateStore = GLOBAL_SAFETY_STATE,
        poll_hz: float = 5.0,
        max_attempts_per_incident: int = 3,
        retry_delay_s: float = 1.0,
        recovery_timeout_s: float = 120.0,
        on_manual_required=None,
    ) -> None:
        if poll_hz <= 0 or max_attempts_per_incident <= 0 or retry_delay_s < 0 or recovery_timeout_s <= 0:
            raise ValueError(
                "poll_hz/max_attempts_per_incident/recovery_timeout_s must be positive "
                "and retry_delay_s non-negative"
            )
        self.runtime = runtime
        self.graph_id = graph_id
        self.ctx = ctx
        self.body = body
        self.monitor = monitor
        self.safety_state = safety_state
        self.poll_interval = 1.0 / poll_hz
        self.max_attempts_per_incident = max_attempts_per_incident
        self.retry_delay_s = retry_delay_s
        self.recovery_timeout_s = recovery_timeout_s
        self.on_manual_required = on_manual_required
        self.snapshot = RecoveryControllerSnapshot(status="stopped")
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._approval_event = asyncio.Event()
        self._approval_incident: str | None = None
        self._manual_notified_incident: str | None = None
        self._incident_attempts: dict[str, int] = {}
        self._run_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self.snapshot.status = "running"
        self._task = asyncio.create_task(self._run(), name=f"robotenon-recovery-{self.graph_id}")

    async def stop(self) -> None:
        self._stopping = True
        self._approval_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.snapshot.status = "stopped"

    async def approve(self, incident_id: str | None = None) -> None:
        """Approve the currently waiting manual recovery."""
        current = await async_safety_call(self.safety_state, "snapshot")
        expected = str(current.incident_id) if current.incident_id is not None else None
        supplied = incident_id or expected
        if expected is not None and supplied != expected:
            raise ValueError(f"approval incident {supplied!r} does not match active incident {expected!r}")
        self._approval_incident = supplied
        self._approval_event.set()

    async def run_once(self, *, approved: bool = False):
        """Process the current incident once; useful for deterministic tests."""
        async with self._run_lock:
            return await self._run_once_locked(approved=approved)

    async def _run(self) -> None:
        try:
            while not self._stopping:
                await self.run_once()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise

    async def _run_once_locked(self, *, approved: bool) -> Any | None:
        safety = await async_safety_call(self.safety_state, "snapshot")
        self.snapshot.level = safety.level
        self.snapshot.incident_id = str(safety.incident_id) if safety.incident_id is not None else None
        self.snapshot.updated_at = datetime.now(timezone.utc)
        if safety.level is SafetyLevel.P0:
            self.snapshot.status = "running"
            self.snapshot.last_error = None
            return None

        # P1/P2 do not stop an active graph by default.  Let the normal graph
        # continue unless it has already entered a safety/recovery state;
        # P3/P4 are always eligible for this controller.
        if safety.level.rank < SafetyLevel.P3.rank and getattr(self.ctx, "status", "running") == "running":
            return None
        try:
            graph = self.runtime.graphs[self.graph_id]
        except (AttributeError, KeyError, TypeError) as exc:
            self.snapshot.status = "failed"
            self.snapshot.last_error = f"recovery graph owner is unavailable: {exc}"
            return None
        policy = graph.recovery_for(safety.level)
        if policy is None:
            self.snapshot.status = "failed"
            self.snapshot.last_error = f"no recovery policy for {safety.level.value}"
            return None
        incident = self.snapshot.incident_id or "legacy"
        attempts = self._incident_attempts.get(incident, 0)
        if attempts >= self.max_attempts_per_incident:
            self.snapshot.status = "failed"
            self.snapshot.last_error = "maximum recovery attempts exceeded"
            return None
        if policy.mode == "manual" and not approved:
            if self._approval_incident != incident:
                self.snapshot.status = "waiting_manual"
                callback = self.on_manual_required
                if callback is not None and self._manual_notified_incident != incident:
                    self._manual_notified_incident = incident
                    value = callback(safety)
                    if inspect.isawaitable(value):
                        await value
                return None
            approved = True
        self.snapshot.status = "recovering"
        self._incident_attempts[incident] = attempts + 1
        self.snapshot.attempts = attempts + 1
        try:
            result = await asyncio.wait_for(
                self.runtime.recover(
                    self.graph_id,
                    self.ctx,
                    body=self.body,
                    monitor=self.monitor,
                    approved=approved,
                ),
                timeout=self.recovery_timeout_s,
            )
            self.snapshot.result = result
            if result.success:
                self.snapshot.status = "completed"
                self.snapshot.last_error = None
            else:
                self.snapshot.status = "failed"
                self.snapshot.last_error = result.error or result.status
            return result
        except Exception as exc:
            self.snapshot.status = "failed"
            self.snapshot.last_error = str(exc)
            return None
        finally:
            # A manual acknowledgement authorizes one bounded recovery
            # attempt, not an unbounded retry loop.  If the attempt fails or
            # the fault remains active, require a fresh operator approval.
            if policy.mode == "manual":
                self._approval_incident = None
            if self.retry_delay_s:
                await asyncio.sleep(self.retry_delay_s)


__all__ = [
    "RecoveryController",
    "RecoveryControllerSnapshot",
    "RecoveryControllerStatus",
]
