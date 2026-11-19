from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..runtime.safety import max_safety_level
from ..runtime.registry import register_module
from ..schemas import (
    Action,
    ActionChunk,
    ActionType,
    EvaluationDecision,
    EvaluationDecisionType,
    EvaluationInput,
    QueueOperation,
    SafetyLevel,
    SafetySignal,
    SafetyStateSnapshot,
    TaskItem,
    EnvironmentFeedback,
)
from .base import BaseModule, ModuleContext, ModuleResult


@register_module("echo", version="1.0.0", kind="debug", description="Return the shared context values unchanged.")
class EchoModule(BaseModule):
    def __init__(self, prefix: str = ""):
        self.prefix = prefix

    async def invoke(self, arguments: Mapping[str, Any], context: ModuleContext) -> ModuleResult:
        return ModuleResult(ok=True, output={"prefix": self.prefix, "arguments": dict(arguments)})

    async def run(self, ctx) -> dict[str, Any]:
        """Graph-native entry point receiving the shared context directly."""
        return {"prefix": self.prefix, "values": dict(ctx.inputs)}


@register_module(
    "wait-agent",
    version="1.0.0",
    kind="agent",
    description="Deterministic wait policy for framework smoke tests.",
)
class WaitAgent(BaseModule):
    """Small reference policy used by examples and smoke tests."""

    def __init__(self, *, payload: dict[str, Any] | None = None, timeout_ms: int = 1000):
        self.payload = payload or {"duration_ms": 1}
        self.timeout_ms = timeout_ms

    async def act(
        self,
        feedback: EnvironmentFeedback,
        *,
        task: TaskItem | None = None,
        context: Any = None,
    ) -> Action:
        return Action(
            session_id=feedback.session_id,
            action_type=ActionType.WAIT,
            payload={
                **self.payload,
                "goal": task.goal if task is not None else None,
            },
            timeout_ms=self.timeout_ms,
        )

    async def run(self, ctx: Any) -> dict[str, Any]:
        if ctx.feedback is None:
            raise ValueError("wait-agent run requires ctx.feedback")
        action = await self.act(
            ctx.feedback,
            task=getattr(ctx, "task_item", None),
            context=ctx,
        )
        ctx.action = action
        ctx.action_chunk = ActionChunk(
            session_id=action.session_id,
            actions=[action],
            max_duration_ms=action.timeout_ms,
        )
        return {"action": action, "action_chunk": ctx.action_chunk}

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ModuleContext,
    ) -> ModuleResult:
        if context.feedback is None:
            raise ValueError("wait-agent invoke requires ModuleContext.feedback")
        call_context = arguments.get("context", context)
        return ModuleResult(
            ok=True,
            output=await self.act(
                context.feedback,
                task=context.task,
                context=call_context,
            ),
        )


@register_module(
    "terminal-evaluator",
    version="1.0.0",
    kind="evaluator",
    description="Use terminal feedback/error to decide continue, complete or abort.",
)
class TerminalEvaluator(BaseModule):
    """Reference evaluator for the JSON graph loop."""

    async def evaluate(self, item: EvaluationInput) -> EvaluationDecision:
        feedback = item.feedback
        safety_signals = [
            SafetySignal(
                code=signal.name,
                level=signal.safety_level,
                source=signal.source,
                message=str(signal.value) if signal.value is not None else None,
                timestamp=signal.timestamp,
                metadata=signal.metadata,
            )
            for signal in feedback.signals
            if signal.safety_level is not None
        ]
        safety_level = max_safety_level(
            feedback.safety_level,
            *(signal.level for signal in safety_signals),
        )
        task_success: bool | None = None
        queue_operation = QueueOperation.HOLD
        task_item_success: bool | None = None
        if safety_level.rank >= SafetyLevel.P3.rank:
            decision = EvaluationDecisionType.ABORT
            reason_code = "safety_stop"
            queue_operation = QueueOperation.CLEAR
            task_success = False
            task_item_success = False if item.task is not None else None
        elif feedback.error or (feedback.terminal and feedback.success is False):
            decision = EvaluationDecisionType.ABORT
            reason_code = "environment_failure"
            queue_operation = QueueOperation.CLEAR
            task_success = False
        elif feedback.terminal and feedback.success:
            decision = EvaluationDecisionType.COMPLETE
            reason_code = "environment_success"
            queue_operation = QueueOperation.NEXT
            task_success = True
            task_item_success = True if item.task is not None else None
        elif feedback.action_status.value in {"failed", "rejected"}:
            decision = EvaluationDecisionType.RETRY
            reason_code = "action_failed"
            queue_operation = QueueOperation.RETRY
            task_item_success = False if item.task is not None else None
        elif item.task is not None and feedback.action_status.value == "succeeded":
            decision = EvaluationDecisionType.CONTINUE
            reason_code = "task_item_success"
            queue_operation = QueueOperation.NEXT
            task_item_success = True
        else:
            decision = EvaluationDecisionType.CONTINUE
            reason_code = "not_terminal"
        return EvaluationDecision(
            session_id=item.session_id,
            decision=decision,
            reason_code=reason_code,
            based_on_feedback_id=feedback.feedback_id,
            based_on_sequence_no=feedback.sequence_no,
            context_revision=item.context_revision,
            task_success=task_success,
            task_item_success=task_item_success,
            safety_level=safety_level,
            safety_signals=safety_signals,
            queue_operation=queue_operation,
        )

    async def run(self, ctx: Any) -> dict[str, Any]:
        if ctx.feedback is None:
            raise ValueError("terminal evaluator run requires ctx.feedback")
        item = EvaluationInput(
            session_id=ctx.feedback.session_id,
            feedback=ctx.feedback,
            context_revision=int(getattr(ctx, "values", {}).get("context_revision", 0)),
            action=getattr(ctx, "action", None),
            task=getattr(ctx, "task_item", None),
        )
        decision = await self.evaluate(item)
        ctx.decision = decision
        if decision.safety_level.rank > SafetyLevel.P0.rank:
            ctx.safety = getattr(ctx, "safety", None) or SafetyStateSnapshot()
            ctx.safety.level = decision.safety_level
        should_continue = decision.task_success is not True and decision.decision not in {
            EvaluationDecisionType.ABORT,
            EvaluationDecisionType.PAUSE,
            EvaluationDecisionType.COMPLETE,
        }
        return {"decision": decision, "continue": should_continue}

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ModuleContext,
    ) -> ModuleResult:
        raw = arguments.get("item")
        if isinstance(raw, EvaluationInput):
            item = raw
        else:
            feedback = arguments.get("feedback") or context.feedback
            if feedback is None:
                raise ValueError("terminal evaluator invoke requires feedback")
            item = EvaluationInput(
                session_id=arguments.get("session_id") or feedback.session_id,
                feedback=feedback,
                context_revision=int(arguments.get("context_revision", 0)),
                task=arguments.get("task"),
            )
        return ModuleResult(ok=True, output=await self.evaluate(item))


__all__ = ["EchoModule", "TerminalEvaluator", "WaitAgent"]
