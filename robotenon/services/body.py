from __future__ import annotations

from ..contracts.persistence import SafetyStore

import asyncio
import inspect
import hashlib
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bodies.base import BaseBodyService
from ..contracts.serialization import jsonable
from ..runtime.safety import (
    GLOBAL_SAFETY_STATE,
    SafetyGate,
    async_safety_call,
)
from ..schemas import (
    Action,
    ActionChunk,
    ActionExecution,
    BodyExecutionResult,
    BodyExecutionStatus,
    EnvironmentFeedback,
    EnvironmentHealth,
    SafetyLevel,
    SafetyStateSnapshot,
)
from .action_ledger import ActionExecutionStore, SQLiteActionExecutionStore
from .protocol import BodyExecutor


class InProcessBodyService(BaseBodyService):
    """Resident cerebellum/body implementation with a safety supervisor.

    ``executor`` is the only robot-specific callback.  It may be synchronous
    or asynchronous; synchronous work is moved to a worker thread so the
    high-frequency safety poll remains responsive.  A real body process can
    implement the same ``BodyService`` protocol and keep its watchdog in the
    hardware controller.
    """

    def __init__(
        self,
        executor: BodyExecutor,
        *,
        safety_state: SafetyStore = GLOBAL_SAFETY_STATE,
        poll_hz: float = 100.0,
        safety_gate: SafetyGate | None = None,
        interrupt_level: SafetyLevel = SafetyLevel.P3,
        safe_action_factory=None,
        safe_action_factories: Mapping[SafetyLevel | str, Callable[[SafetyStateSnapshot], Any]] | None = None,
        stop_executor: Callable[[str], Any | Awaitable[Any]] | None = None,
        idempotency_store: ActionExecutionStore | None = None,
        idempotency_db: str | Path | None = None,
        idempotency_store_config: Mapping[str, Any] | None = None,
        stop_timeout_s: float = 0.5,
        heartbeat_ttl_s: float | None = None,
        idempotency_scope: str | None = None,
    ) -> None:
        if poll_hz <= 0 or stop_timeout_s <= 0 or (
            heartbeat_ttl_s is not None and heartbeat_ttl_s <= 0
        ):
            raise ValueError("poll_hz, stop_timeout_s and heartbeat_ttl_s must be positive")
        self.executor = executor
        self.safety_state = safety_state
        self.safety_gate = safety_gate or SafetyGate()
        self.poll_interval = 1.0 / poll_hz
        self.interrupt_level = (
            interrupt_level
            if isinstance(interrupt_level, SafetyLevel)
            else SafetyLevel(str(interrupt_level).upper())
        )
        self.safe_action_factory = safe_action_factory
        self.safe_action_factories = {
            (level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper())): factory
            for level, factory in (safe_action_factories or {}).items()
        }
        # ``asyncio`` cannot forcibly stop a synchronous call running in a
        # worker thread.  Integrations controlling real hardware should supply
        # this callback so the low-level stop/hold command is sent immediately
        # when the watchdog observes P3/P4.
        self.stop_executor = stop_executor
        self.stop_timeout_s = stop_timeout_s
        self.idempotency_scope = idempotency_scope or f"{type(self).__module__}.{type(self).__qualname__}"
        self._stop_task: asyncio.Task[bool] | None = None
        self._stop_failed = False
        configured_ledger = idempotency_db or os.environ.get("ROBOTENON_ACTION_DB")
        if idempotency_store_config is not None and (idempotency_store is not None or idempotency_db is not None):
            raise ValueError("select one action store configuration or injected store")
        self._idempotency_store_config = idempotency_store_config
        if idempotency_store is not None and configured_ledger is not None:
            raise ValueError("pass either idempotency_store or idempotency_db, not both")
        self.idempotency_store = idempotency_store
        self._owns_idempotency_store = False
        self._idempotency_db_path: str | Path | None = None
        if idempotency_store_config is not None:
            from ..storage.registry import build_store
            self.idempotency_store = build_store("actions", idempotency_store_config)
            self._owns_idempotency_store = True
        elif self.idempotency_store is None and configured_ledger:
            self.idempotency_store = SQLiteActionExecutionStore(configured_ledger)
            self._owns_idempotency_store = True
            self._idempotency_db_path = configured_ledger
        self._safety_task: asyncio.Task[None] | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._active_chunk: ActionChunk | None = None
        self._interrupt_reason: str | None = None
        self._interrupt_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._interrupt_lock = asyncio.Lock()
        self._closed = False
        self.stop_errors = 0
        self._handled_safety_revision: int | None = None
        self._heartbeat_ttl = (
            float(heartbeat_ttl_s)
            if heartbeat_ttl_s is not None
            else max(1.0, self.poll_interval * 3.0)
        )
        # Idempotency is scoped to a session.  A caller may legitimately use
        # the same human-readable key (for example ``"grasp-1"``) in two
        # independent episodes; those must not share an acknowledgement.
        self._completed_idempotency: dict[tuple[str, str], ActionExecution] = {}
        self._active_action: Action | None = None
        self._active_action_key: str | None = None
        self._active_action_started: datetime | None = None
        # ``asyncio`` cancellation cannot kill a synchronous SDK call running
        # in a worker thread. Keep a guard so a timed-out driver cannot be
        # followed by a second command while the first call is still alive.
        self._sync_driver_busy = threading.Event()

    async def start(self) -> None:
        if self._closed:
            if self._owns_idempotency_store and self._idempotency_store_config is not None:
                from ..storage.registry import build_store
                self.idempotency_store = build_store("actions", self._idempotency_store_config)
            elif self._owns_idempotency_store and self._idempotency_db_path is not None:
                self.idempotency_store = SQLiteActionExecutionStore(self._idempotency_db_path)
            self._closed = False
        if self._safety_task is None or self._safety_task.done():
            await async_safety_call(
                self.safety_state,
                "heartbeat",
                "body",
                ttl_s=self._heartbeat_ttl,
                failure_level=self.interrupt_level,
            )
            self._safety_task = asyncio.create_task(self._safety_loop(), name="robotenon-body-safety")

    async def execute(self, action_chunk: ActionChunk) -> BodyExecutionResult:
        await self.start()
        if not action_chunk.actions:
            return BodyExecutionResult(
                chunk_id=action_chunk.chunk_id,
                session_id=action_chunk.session_id,
                status=BodyExecutionStatus.SUCCEEDED,
                executed_actions=0,
            )
        if self._sync_driver_busy.is_set() or self._stop_failed:
            return BodyExecutionResult(
                chunk_id=action_chunk.chunk_id,
                session_id=action_chunk.session_id,
                status=BodyExecutionStatus.REJECTED,
                error="body driver is busy or its last stop was not confirmed",
            )
        async with self._lock:
            if self._sync_driver_busy.is_set() or self._stop_failed:
                return BodyExecutionResult(chunk_id=action_chunk.chunk_id, session_id=action_chunk.session_id,
                    status=BodyExecutionStatus.REJECTED, error="body driver is busy or its last stop was not confirmed")
            if self._active_task is not None and not self._active_task.done():
                raise RuntimeError("body is already executing an action chunk")
            self._active_task = asyncio.current_task()
            self._active_chunk = action_chunk
            self._interrupt_event.clear()
            self._interrupt_reason = None
            self._stop_task = None
            executed = 0
            last_feedback = None
            action_results: list[ActionExecution] = []
            started_monotonic = time.monotonic()
            current_action: Action | None = None
            current_key: str | None = None
            current_started: datetime | None = None
            try:
                for action in action_chunk.actions:
                    self._validate_action(action_chunk, action)
                    key = action.idempotency_key or str(action.action_id)
                    cache_key = (str(action.session_id), key)
                    request_hash = self._request_hash(action)
                    cached = self._completed_idempotency.get(cache_key)
                    if cached is None and self.idempotency_store is not None:
                        cached = await self._load_idempotency(action.session_id, key)
                        if cached is not None:
                            self._completed_idempotency[cache_key] = cached
                    if cached is not None and cached.status == "succeeded":
                        if cached.metadata.get("request_hash") != request_hash:
                            return BodyExecutionResult(
                                chunk_id=action_chunk.chunk_id, session_id=action_chunk.session_id,
                                status=BodyExecutionStatus.REJECTED, executed_actions=executed,
                                error="idempotency_conflict: key has a different or unverifiable request",
                                action_results=action_results + [ActionExecution(
                                    action_id=action.action_id, idempotency_key=key,
                                    status="rejected", error="idempotency_conflict",
                                )],
                            )
                        action_results.append(cached)
                        continue
                    snapshot = await self._safety_snapshot()
                    if snapshot.level.rank >= self.interrupt_level.rank:
                        self._interrupt_reason = f"safety_level_{snapshot.level.value}"
                        self._interrupt_event.set()
                        raise asyncio.CancelledError
                    if self._interrupt_event.is_set():
                        raise asyncio.CancelledError
                    gate = self.safety_gate.check(snapshot, action)
                    if not gate["allowed"]:
                        return BodyExecutionResult(
                            chunk_id=action_chunk.chunk_id, session_id=action_chunk.session_id,
                            status=BodyExecutionStatus.REJECTED, executed_actions=executed,
                            safety_level=snapshot.level, error=gate["reason"],
                            action_results=action_results + [ActionExecution(
                                action_id=action.action_id, idempotency_key=key,
                                status="rejected", error=gate["reason"],
                            )],
                        )
                    action_started = datetime.now(timezone.utc)
                    current_action = action
                    current_key = key
                    current_started = action_started
                    self._active_action = action
                    self._active_action_key = key
                    self._active_action_started = action_started
                    if action.deadline_ns is not None and time.time_ns() >= action.deadline_ns:
                        await self._ensure_stop(f"action_deadline:{action.action_id}")
                        await self._raise_deadline_signal(
                            f"action_deadline:{action.action_id}",
                            "action deadline already expired",
                        )
                        action_results.append(ActionExecution(
                            action_id=action.action_id,
                            idempotency_key=key,
                            status="deadline_exceeded",
                            started_at=action_started,
                            ended_at=datetime.now(timezone.utc),
                            error="action deadline already expired",
                        ))
                        return BodyExecutionResult(
                            chunk_id=action_chunk.chunk_id,
                            session_id=action_chunk.session_id,
                            status=BodyExecutionStatus.FAILED,
                            executed_actions=executed,
                            safety_level=(await self._safety_snapshot()).level,
                            error="action deadline already expired",
                            action_results=action_results,
                            deadline_exceeded=True,
                        )
                    action_deadline = action_started.timestamp() + action.timeout_ms / 1000.0
                    chunk_remaining = action_chunk.max_duration_ms / 1000.0 - (time.monotonic() - started_monotonic)
                    # ``deadline_ns`` is an absolute Unix-nanosecond deadline
                    # and takes precedence over the relative timeout.  Also
                    # honour a chunk that was created in the past instead of
                    # granting it a fresh duration when delivery is delayed.
                    chunk_created_at = action_chunk.created_at
                    if chunk_created_at.tzinfo is None:
                        chunk_created_at = chunk_created_at.replace(tzinfo=timezone.utc)
                    chunk_absolute_remaining = (
                        chunk_created_at.timestamp() + action_chunk.max_duration_ms / 1000.0 - time.time()
                    )
                    chunk_remaining = min(chunk_remaining, chunk_absolute_remaining)
                    absolute_remaining = float("inf")
                    if action.deadline_ns is not None:
                        absolute_remaining = (action.deadline_ns - time.time_ns()) / 1_000_000_000.0
                    timeout = min(action.timeout_ms / 1000.0, chunk_remaining, absolute_remaining)
                    if timeout <= 0:
                        await self._ensure_stop(f"action_chunk_deadline:{action_chunk.chunk_id}")
                        await self._raise_deadline_signal(
                            f"action_chunk_deadline:{action_chunk.chunk_id}",
                            "action chunk deadline exceeded",
                        )
                        action_results.append(ActionExecution(
                            action_id=action.action_id,
                            idempotency_key=key,
                            status="deadline_exceeded",
                            started_at=action_started,
                            ended_at=datetime.now(timezone.utc),
                            error="action chunk deadline exceeded",
                        ))
                        return BodyExecutionResult(
                            chunk_id=action_chunk.chunk_id,
                            session_id=action_chunk.session_id,
                            status=BodyExecutionStatus.FAILED,
                            executed_actions=executed,
                            safety_level=(await self._safety_snapshot()).level,
                            error="action chunk deadline exceeded",
                            action_results=action_results,
                            deadline_exceeded=True,
                        )
                    try:
                        result = await asyncio.wait_for(self._invoke_executor(action), timeout=timeout)
                    except asyncio.TimeoutError:
                        if chunk_remaining <= min(action.timeout_ms / 1000.0, absolute_remaining):
                            deadline_code = f"action_chunk_deadline:{action_chunk.chunk_id}"
                            deadline_error = f"action chunk deadline exceeded after {action_chunk.max_duration_ms} ms"
                        elif absolute_remaining <= action.timeout_ms / 1000.0:
                            deadline_code = f"action_deadline:{action.action_id}"
                            deadline_error = "action absolute deadline exceeded"
                        else:
                            deadline_code = f"action_deadline:{action.action_id}"
                            deadline_error = f"action timeout after {action.timeout_ms} ms"
                        await self._ensure_stop(deadline_code)
                        await self._raise_deadline_signal(
                            deadline_code,
                            deadline_error,
                        )
                        action_results.append(ActionExecution(
                            action_id=action.action_id,
                            idempotency_key=key,
                            status="deadline_exceeded",
                            started_at=action_started,
                            ended_at=datetime.now(timezone.utc),
                            error=deadline_error,
                        ))
                        return BodyExecutionResult(
                            chunk_id=action_chunk.chunk_id,
                            session_id=action_chunk.session_id,
                            status=BodyExecutionStatus.FAILED,
                            executed_actions=executed,
                            safety_level=(await self._safety_snapshot()).level,
                            error=deadline_error,
                            action_results=action_results,
                            deadline_exceeded=True,
                        )
                    # A local executor may return the corresponding
                    # EnvironmentFeedback; preserve it without making
                    # feedback a requirement of the Body protocol (remote
                    # bodies may only acknowledge motor execution).
                    if isinstance(result, EnvironmentFeedback):
                        if result.session_id != action_chunk.session_id:
                            raise ValueError("body executor returned feedback for a different session")
                        if result.action_id is not None and result.action_id != action.action_id:
                            raise ValueError("body executor feedback action_id does not match the executed action")
                        last_feedback = result
                    executed += 1
                    execution = ActionExecution(
                        action_id=action.action_id,
                        idempotency_key=key,
                        status="succeeded",
                        started_at=action_started,
                        ended_at=datetime.now(timezone.utc),
                        metadata={
                            "request_hash": request_hash,
                            "deadline_unix_s": action_deadline,
                            "relative_deadline_unix_s": action_deadline,
                            "absolute_deadline_ns": action.deadline_ns,
                        },
                    )
                    action_results.append(execution)
                    self._completed_idempotency[cache_key] = execution
                    if self.idempotency_store is not None:
                        await self._save_idempotency(action.session_id, execution)
                    if len(self._completed_idempotency) > 4096:
                        self._completed_idempotency.pop(next(iter(self._completed_idempotency)))
                    # Environment-backed executors can report terminal/success
                    # after an action. Stop consuming the chunk at that boundary:
                    # do not execute the remainder of a predicted chunk after
                    # the episode has already ended.
                    if isinstance(last_feedback, EnvironmentFeedback) and last_feedback.terminal:
                        break
                return BodyExecutionResult(
                    chunk_id=action_chunk.chunk_id,
                    session_id=action_chunk.session_id,
                    status=BodyExecutionStatus.SUCCEEDED,
                    executed_actions=executed,
                    feedback=self._feedback_for_result(last_feedback),
                    action_results=action_results,
                )
            except Exception as exc:
                if current_action is not None and current_key is not None and not any(
                    item.action_id == current_action.action_id for item in action_results
                ):
                    action_results.append(ActionExecution(
                        action_id=current_action.action_id,
                        idempotency_key=current_key,
                        status="failed",
                        started_at=current_started,
                        ended_at=datetime.now(timezone.utc),
                        error=str(exc),
                    ))
                return BodyExecutionResult(
                    chunk_id=action_chunk.chunk_id,
                    session_id=action_chunk.session_id,
                    status=BodyExecutionStatus.FAILED,
                    executed_actions=executed,
                    safety_level=(await self._safety_snapshot()).level,
                    error=str(exc),
                    action_results=action_results,
                )
            except asyncio.CancelledError:
                external_cancel = self._interrupt_reason is None
                reason = self._interrupt_reason or "body_execution_cancelled"
                self._interrupt_reason = reason
                await self._ensure_stop(reason)
                snapshot = await self._safety_snapshot()
                if external_cancel:
                    raise
                if current_action is not None and current_key is not None and not any(
                    item.action_id == current_action.action_id for item in action_results
                ):
                    action_results.append(ActionExecution(
                        action_id=current_action.action_id,
                        idempotency_key=current_key,
                        status="interrupted",
                        started_at=current_started,
                        ended_at=datetime.now(timezone.utc),
                        error=reason,
                    ))
                safe_error = None
                try:
                    await self._run_safe_action(snapshot)
                except Exception as exc:  # fail closed but preserve the stop result
                    safe_error = str(exc)
                if safe_error:
                    reason = f"{reason};safe_action_failed:{safe_error}"
                return BodyExecutionResult(
                    chunk_id=action_chunk.chunk_id,
                    session_id=action_chunk.session_id,
                    status=BodyExecutionStatus.INTERRUPTED,
                    executed_actions=executed,
                    interrupted=True,
                    safety_level=snapshot.level,
                    error=reason,
                    action_results=action_results,
                )
            finally:
                self._active_task = None
                self._active_chunk = None
                self._active_action = None
                self._active_action_key = None
                self._active_action_started = None
                self._interrupt_event.clear()

    async def interrupt(
        self,
        reason: str,
        *,
        safe_action: ActionChunk | None = None,
    ) -> BodyExecutionResult:
        async with self._interrupt_lock:
            self._interrupt_reason = reason
            self._interrupt_event.set()
            if self.stop_executor is not None:
                await self._ensure_stop(reason)
            active = self._active_task
            current = asyncio.current_task()
            if active is not None and active is not current and not active.done():
                active.cancel()
            if self._active_chunk is not None:
                chunk = self._active_chunk
                action_results: list[ActionExecution] = []
                if self._active_action is not None and self._active_action_key is not None:
                    action_results.append(ActionExecution(
                        action_id=self._active_action.action_id,
                        idempotency_key=self._active_action_key,
                        status="interrupted",
                        started_at=self._active_action_started,
                        ended_at=datetime.now(timezone.utc),
                        error=reason,
                    ))
                return BodyExecutionResult(
                    chunk_id=chunk.chunk_id,
                    session_id=chunk.session_id,
                    status=BodyExecutionStatus.INTERRUPTED,
                    interrupted=True,
                    safety_level=(await self._safety_snapshot()).level,
                    error=reason,
                    action_results=action_results,
                )
            if safe_action is not None:
                try:
                    await self._execute_safe_chunk(safe_action)
                except Exception:
                    self.stop_errors += 1
            else:
                try:
                    await self._run_safe_action(await self._safety_snapshot())
                except Exception:
                    self.stop_errors += 1
            return BodyExecutionResult(
                chunk_id=None,
                session_id=None,
                status=BodyExecutionStatus.INTERRUPTED,
                interrupted=True,
                safety_level=(await self._safety_snapshot()).level,
                error=reason,
            )

    async def health(self) -> EnvironmentHealth:
        active = self._active_chunk
        watchdog_stopped = self._safety_task is None or self._safety_task.done()
        return EnvironmentHealth(
            ready=not self._closed and not watchdog_stopped and not self._stop_failed,
            degraded=active is not None or self._sync_driver_busy.is_set() or watchdog_stopped or self._stop_failed,
            message=(
                f"executing:{active.chunk_id}" if active is not None
                else "low_level_stop_failed" if self._stop_failed
                else "driver_busy_after_timeout" if self._sync_driver_busy.is_set()
                else "body safety watchdog stopped" if watchdog_stopped
                else None
            ),
        )

    async def close(self) -> None:
        self._closed = True
        active = self._active_task
        if active is not None and not active.done():
            await self.interrupt("body_service_close")
            if active is not asyncio.current_task():
                done, _ = await asyncio.wait({active}, timeout=self.stop_timeout_s)
                if not done:
                    raise TimeoutError("body execution did not stop during close")
        elif self._sync_driver_busy.is_set():
            await self._ensure_stop("body_service_close")
        if self._safety_task is not None:
            self._safety_task.cancel()
            try:
                await self._safety_task
            except asyncio.CancelledError:
                pass
            self._safety_task = None
        await async_safety_call(self.safety_state, "clear_heartbeat", "body")
        if self._owns_idempotency_store and self.idempotency_store is not None:
            close = getattr(self.idempotency_store, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        deadline = time.monotonic() + self.stop_timeout_s
        while self._sync_driver_busy.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(.01, self.stop_timeout_s))
        if self._sync_driver_busy.is_set():
            raise TimeoutError("body driver is still running after close deadline")

    @staticmethod
    def _validate_action(action_chunk: ActionChunk, action: Action) -> None:
        if action.session_id != action_chunk.session_id:
            raise ValueError("all actions in a chunk must belong to the chunk session")
        if action.timeout_ms <= 0:
            raise ValueError("action timeout must be positive")

    @staticmethod
    def _feedback_for_result(feedback: EnvironmentFeedback | None) -> EnvironmentFeedback | None:
        """Keep Body acknowledgements serialisable without retaining sensor buffers.

        The executor may return a raw environment feedback so the Body can
        stop a chunk immediately on terminal/success. The observation node is
        still responsible for ingesting the full media payload into the
        observation cache, so the Body result only needs the bounded feedback
        metadata/state summary.
        """

        if feedback is None:
            return None
        observation = feedback.observation
        copy_observation = getattr(observation, "model_copy", None)
        if callable(copy_observation):
            observation = copy_observation(update={"frames": []})
        else:
            observation = observation.copy(update={"frames": []})
        copy_feedback = getattr(feedback, "model_copy", None)
        if callable(copy_feedback):
            return copy_feedback(update={"observation": observation, "frames": {}})
        return feedback.copy(update={"observation": observation, "frames": {}})

    async def _safety_snapshot(self) -> SafetyStateSnapshot:
        """Read safety state without blocking a Body event loop."""

        return await async_safety_call(self.safety_state, "snapshot")

    def _request_hash(self, action: Action) -> str:
        request = {"version": 1, "scope": self.idempotency_scope,
                   "action_type": action.action_type, "payload": action.payload}
        encoded = json.dumps(jsonable(request), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def _ensure_stop(self, reason: str) -> bool:
        # Watchdog, caller cancellation and close share the same stop attempt.
        # Shield it so a second cancellation cannot cancel hardware cleanup.
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._invoke_stop(reason))
        return await asyncio.shield(self._stop_task)

    async def _invoke_stop(self, reason: str) -> bool:
        if self.stop_executor is None:
            return True
        try:
            if inspect.iscoroutinefunction(self.stop_executor):
                stop_result = self.stop_executor(reason)
            else:
                # A synchronous vendor stop API must not block the watchdog's
                # event loop.  A timed-out worker cannot be killed, so the
                # failure is escalated to latched P4 below.
                stop_result = asyncio.to_thread(self.stop_executor, reason)
            if inspect.isawaitable(stop_result):
                stop_task = asyncio.ensure_future(stop_result)
                stop_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                done, _ = await asyncio.wait({stop_task}, timeout=self.stop_timeout_s)
                if not done:
                    stop_task.cancel()
                    raise TimeoutError("low-level stop deadline exceeded")
                stop_result = stop_task.result()
            if stop_result is False:
                raise RuntimeError("driver rejected stop")
            return True
        except Exception:
            # A failed low-level stop must not prevent cancellation of the
            # cooperative task. Expose the count and raise a latched P4 so a
            # supervisor/hardware controller cannot continue in normal mode.
            self.stop_errors += 1
            self._stop_failed = True
            try:
                await async_safety_call(
                    self.safety_state,
                    "raise_level",
                    SafetyLevel.P4,
                    code="body.stop_failed",
                    source="body",
                    message=f"low-level stop failed: {reason}",
                )
            except Exception:
                pass
            return False

    async def _raise_deadline_signal(self, code: str, message: str) -> None:
        """Turn a missed physical deadline into a latched protective pause."""
        try:
            await async_safety_call(
                self.safety_state,
                "raise_level",
                SafetyLevel.P3,
                code=f"body.deadline.{code}",
                source="body",
                message=message,
            )
        except Exception:
            # The action result remains authoritative for the caller.  A
            # broken safety backend must not hide the deadline diagnostic.
            pass

    async def _invoke_executor(self, action) -> Any:
        if inspect.iscoroutinefunction(self.executor):
            result = self.executor(action)
        else:
            if self._sync_driver_busy.is_set():
                raise RuntimeError("body driver is still completing a previous action")

            def call_sync():
                try:
                    return self.executor(action)
                finally:
                    self._sync_driver_busy.clear()

            self._sync_driver_busy.set()
            try:
                future = asyncio.get_running_loop().run_in_executor(None, call_sync)
            except BaseException:
                self._sync_driver_busy.clear()
                raise
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            result = await asyncio.shield(future)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _load_idempotency(self, session_id: Any, key: str) -> ActionExecution | None:
        """Use an async store when available, otherwise isolate sync I/O."""

        store = self.idempotency_store
        if store is None:
            return None
        method = getattr(store, "load_async", None)
        if callable(method):
            value = method(session_id, key)
            return await value if inspect.isawaitable(value) else value
        return await asyncio.to_thread(store.load, session_id, key)

    async def _save_idempotency(self, session_id: Any, execution: ActionExecution) -> None:
        """Persist an acknowledgement without blocking the action loop."""

        store = self.idempotency_store
        if store is None:
            return
        method = getattr(store, "save_async", None)
        if callable(method):
            value = method(session_id, execution)
            if inspect.isawaitable(value):
                await value
            return
        await asyncio.to_thread(store.save, session_id, execution)

    async def _safety_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.poll_interval)
                await async_safety_call(
                    self.safety_state,
                    "heartbeat",
                    "body",
                    ttl_s=self._heartbeat_ttl,
                    failure_level=self.interrupt_level,
                )
                snapshot = await self._safety_snapshot()
                if snapshot.level.rank >= self.interrupt_level.rank:
                    if snapshot.revision != self._handled_safety_revision:
                        self._handled_safety_revision = snapshot.revision
                        await self.interrupt(f"safety_level_{snapshot.level.value}")
        except asyncio.CancelledError:
            raise

    async def _run_safe_action(self, snapshot: SafetyStateSnapshot) -> None:
        factory = self.safe_action_factories.get(snapshot.level)
        if factory is None:
            eligible = [
                (level.rank, candidate)
                for level, candidate in self.safe_action_factories.items()
                if level.rank <= snapshot.level.rank
            ]
            if eligible:
                factory = max(eligible, key=lambda item: item[0])[1]
        if factory is None:
            factory = self.safe_action_factory
        if factory is None:
            return
        safe_action = factory(snapshot)
        if inspect.isawaitable(safe_action):
            safe_action = await safe_action
        if safe_action is None:
            return
        if isinstance(safe_action, Action):
            safe_action = ActionChunk(session_id=safe_action.session_id, actions=[safe_action])
        if isinstance(safe_action, ActionChunk):
            await self._execute_safe_chunk(safe_action)

    async def _execute_safe_chunk(self, chunk: ActionChunk) -> None:
        for action in chunk.actions:
            await asyncio.wait_for(self._invoke_executor(action), timeout=self.stop_timeout_s)


__all__ = ["InProcessBodyService"]
