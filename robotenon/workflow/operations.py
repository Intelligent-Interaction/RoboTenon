"""Typed service operations, separate from graph traversal and checkpointing."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from ..contracts.context import GraphContext
from ..envs.base.protocol import EnvironmentSession
from ..envs.runtime import environment
from ..runtime.safety import (
    SafetyGate,
    async_reconcile_safety_snapshot,
    async_safety_call,
)
from ..schemas import (
    ActionChunk,
    BodyExecutionResult,
    EnvironmentFeedback,
    MonitorReport,
)
from ..services.protocol import BodyService, EnvironmentService, MonitorService


class ServiceOperations:
    def __init__(self, safety_state, safety_gate=None):
        self.safety_state = safety_state
        self.safety_gate = safety_gate or SafetyGate()

    def check_action(self, action):
        result = self.safety_gate.check(self.safety_state.snapshot(), action)
        if not result["allowed"]:
            raise PermissionError(result["reason"])

    async def check_action_async(self, action):
        """Async-safe action gate for SQLite-backed safety stores."""

        snapshot = await async_safety_call(self.safety_state, "snapshot")
        result = self.safety_gate.check(snapshot, action)
        if not result["allowed"]:
            raise PermissionError(result["reason"])
        return snapshot

    async def execute(
        self,
        operation: str,
        ctx: GraphContext,
        env: EnvironmentSession | EnvironmentService | None,
        body: BodyService | None,
        monitor: MonitorService | None,
    ) -> Any:
        """Execute a service operation without registering a service as a Module.

        The graph only knows the transport-agnostic protocols.  In the first
        implementation these are in-process objects; a future HTTP/UDS client
        can be injected without changing a graph manifest.  Environment
        operations are intentionally kept separate from body and monitor
        operations so a workflow can run with only the service clients it
        needs (for example, a monitor can request ``env.observe`` while no
        action is being executed).
        """

        if operation == "graph.stop":
            # The runtime commits the graph terminal marker with this result.
            return {"operation": operation}

        if operation.startswith("env."):
            # System nodes use the same process-resident handle as modules.
            # ``env`` is the handle captured by GraphRuntime at run start;
            # graph nodes never receive a separate environment input.
            if env is None:
                env = environment
            if operation == "env.reset":
                result = env.reset(ctx)
            elif operation == "env.observe":
                observe = getattr(env, "observe", None)
                if not callable(observe):
                    raise RuntimeError(
                        "env.observe requires an environment client implementing observe()"
                    )
                result = observe()
            elif operation == "env.close":
                result = env.close()
                if inspect.isawaitable(result):
                    await result
                return {"operation": operation, "closed": True}
            else:  # Graph definition validation normally catches this first.
                raise ValueError(f"unsupported system operation: {operation}")
            result = await result if inspect.isawaitable(result) else result
            self._record_feedback(ctx, operation, result)
            return {"operation": operation, "feedback": result}

        if operation.startswith("body."):
            if body is None:
                raise RuntimeError(
                    f"system node {operation!r} requires body=BodyService"
                )
            if operation == "body.execute":
                chunk = ctx.action_chunk
                if chunk is None and ctx.action is not None:
                    chunk = ActionChunk(
                        session_id=ctx.action.session_id, actions=[ctx.action]
                    )
                    ctx.action_chunk = chunk
                if chunk is None:
                    raise RuntimeError(
                        "body.execute requires ctx.action_chunk or ctx.action"
                    )
                for action in chunk.actions:
                    await self.check_action_async(action)
                result = body.execute(chunk)
            elif operation == "body.interrupt":
                reason = str(
                    ctx.inputs.get("interrupt_reason", "workflow_requested_interrupt")
                )
                result = body.interrupt(reason)
            else:  # Graph definition validation normally catches this first.
                raise ValueError(f"unsupported system operation: {operation}")
            result = await result if inspect.isawaitable(result) else result
            if not isinstance(result, BodyExecutionResult):
                raise TypeError(f"{operation} must return BodyExecutionResult")
            ctx.body_result = result
            if result.feedback is not None:
                self._record_feedback(ctx, operation, result.feedback)
            return {"operation": operation, "body_result": result}

        if operation.startswith("monitor."):
            if monitor is None:
                raise RuntimeError(
                    f"system node {operation!r} requires monitor=MonitorService/MonitorRuntime"
                )
            if operation == "monitor.check":
                check = getattr(monitor, "check", None)
                if not callable(check):
                    raise RuntimeError(
                        "monitor.check requires a monitor client implementing check()"
                    )
                result = check(ctx.feedback, context=ctx)
            elif operation == "monitor.latest":
                latest = getattr(monitor, "latest", None)
                if not callable(latest):
                    raise RuntimeError(
                        "monitor.latest requires a monitor client implementing latest()"
                    )
                result = latest()
            elif operation == "monitor.request" or operation.startswith(
                "monitor.request."
            ):
                request_fn = getattr(monitor, "request", None)
                if not callable(request_fn):
                    raise RuntimeError(
                        "monitor.request requires a monitor client implementing request()"
                    )
                suffix = operation[len("monitor.request.") :]
                name = suffix or ctx.inputs.get("monitor_name")
                request_payload = ctx.inputs.get("monitor_request", {})
                if request_payload is None:
                    request_payload = {}
                if not isinstance(request_payload, Mapping):
                    raise TypeError("ctx.values['monitor_request'] must be an object")
                request_payload = dict(request_payload)
                result = request_fn(
                    name,
                    request_payload,
                    feedback=ctx.feedback,
                    context=ctx,
                )
            else:  # Graph definition validation normally catches this first.
                raise ValueError(f"unsupported system operation: {operation}")
            result = await result if inspect.isawaitable(result) else result
            if result is not None and not isinstance(result, MonitorReport):
                raise TypeError(f"{operation} must return MonitorReport or None")
            if result is not None:
                ctx.monitor_report = result
                # In-process MonitorRuntime/SafetyMonitorService already
                # publish into this SafetyStateStore.  A remote client does
                # not, so apply its serialized report at the Workflow edge.
                if not getattr(monitor, "publishes_safety", False):
                    await self._apply_monitor_report(result)
            ctx.safety = await async_safety_call(self.safety_state, "snapshot")
            return {
                "operation": operation,
                "monitor_report": result,
                "safety": ctx.safety,
            }

        if operation == "safety.snapshot":
            ctx.safety = await async_safety_call(self.safety_state, "snapshot")
            return {"operation": operation, "safety": ctx.safety}

        # Graph definition validation normally catches this first, but this
        # branch keeps direct programmatic GraphDefinition construction safe.
        raise ValueError(f"unsupported system operation: {operation}")

    @staticmethod
    def _record_feedback(ctx: GraphContext, operation: str, result: Any) -> None:
        if not isinstance(result, EnvironmentFeedback):
            raise TypeError(f"{operation} must return EnvironmentFeedback")
        if ctx.session_id is not None and ctx.session_id != result.session_id:
            raise ValueError(f"{operation} returned feedback for a different session")
        ctx.feedback = result
        ctx.session_id = result.session_id

    async def _apply_monitor_report(self, report: MonitorReport) -> None:
        """Publish a report returned by a remote monitor client.

        ``SafetyMonitorService`` already publishes its own reports.  This
        idempotent update is needed when Workflow talks to a separate monitor
        process over RPC and receives only the serialized report.
        """
        await async_reconcile_safety_snapshot(
            self.safety_state,
            report.source or "monitor",
            report.safety_level,
            report.signals,
            source=report.source or "monitor",
            reason=report.reason,
            synthetic_code=f"{report.source or 'monitor'}.compliance",
        )
