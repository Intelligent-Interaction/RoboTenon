"""Graph nodes for native benchmark policy calls and terminal results."""
from __future__ import annotations

from collections.abc import Mapping
import numpy as np

from ..modules.base import BaseModule
from ..runtime.registry import register_module
from ..schemas import Action, ActionChunk, ActionType, BodyExecutionStatus
from .codec import json_value


def validate_native_action(payload):
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("native action must be a nonempty mapping")
    json_value(payload)  # rejects NaN/Inf and non-serializable objects
    for key in ("actions", "action"):
        if key in payload:
            array = np.asarray(payload[key])
            if array.ndim != 1 or not array.size:
                raise ValueError("one native action must be a vector; use native_actions for a chunk")
            if not np.issubdtype(array.dtype, np.number) or not np.all(np.isfinite(array)):
                raise ValueError("native action vector must be finite and numeric")


@register_module("benchmark_policy", kind="agent")
class BenchmarkPolicy(BaseModule):
    async def run(self, ctx):
        feedback = ctx.feedback
        if feedback is None:
            raise RuntimeError("benchmark_policy requires reset feedback")
        if feedback.terminal:
            raise RuntimeError("policy requested after benchmark termination")
        tool = ctx.params.get("model.vla_tool", [str, None])
        if not tool:
            raise ValueError("model.vla_tool must select a native benchmark policy Tool")
        result = await ctx.call_tool(tool, {"instruction": feedback.metadata["instruction"]})
        if not result.ok:
            raise RuntimeError(result.error or "benchmark policy failed")
        output = result.output
        if not isinstance(output, Mapping):
            raise TypeError("policy must return native_action or native_actions")
        if ("native_action" in output) == ("native_actions" in output):
            raise ValueError("return exactly one of native_action and native_actions")
        payloads = [output["native_action"]] if "native_action" in output else output["native_actions"]
        if not isinstance(payloads, (tuple, list)) or not payloads:
            raise ValueError("native_actions must be a nonempty sequence")
        timeout = ctx.params.get("policy.action_timeout_ms", [int, 10000])
        actions = []
        for i, payload in enumerate(payloads):
            validate_native_action(payload)
            actions.append(Action(
                session_id=feedback.session_id, action_type=ActionType.CUSTOM,
                payload={"format": feedback.metadata["benchmark"],
                         "native_action": json_value(payload)},
                timeout_ms=timeout,
                idempotency_key=f"{feedback.session_id}:{feedback.sequence_no}:{i}",
            ))
        chunk = ActionChunk(session_id=feedback.session_id, actions=actions,
                            max_duration_ms=timeout*len(actions))
        ctx.action = actions[0]
        ctx.action_chunk = chunk
        return {"action": actions[0], "action_chunk": chunk}


@register_module("benchmark_terminal", kind="evaluator")
class BenchmarkTerminal(BaseModule):
    async def run(self, ctx):
        feedback = ctx.feedback
        if feedback is None:
            raise RuntimeError("benchmark_terminal requires feedback")
        body = ctx.body_result
        if body is not None and body.status != BodyExecutionStatus.SUCCEEDED:
            raise RuntimeError(f"benchmark action execution failed: {body.status}")
        if feedback.error:
            raise RuntimeError(f"benchmark environment failed: {feedback.error}")
        if feedback.terminal and ctx.params.get("policy.interface", [str, "callable"]) == "vla":
            result = await ctx.call_tool(ctx.params.get("model.vla_tool", [str, "policy"]), {
                "operation": "end_episode",
                "result": {"status": "completed", "metrics": feedback.info.get("benchmark_metrics", {}),
                           "steps": feedback.info["env_steps"], "elapsed_sec": feedback.info.get("elapsed_sec")},
            })
            if not result.ok:
                raise RuntimeError(result.error or "VLA end_episode failed")
        return {"continue": not feedback.terminal, "success": feedback.success is True,
                "native_case": feedback.metadata["native_case"],
                "reason": feedback.info.get("reason"),
                "env_steps": feedback.info["env_steps"], "elapsed_sec": feedback.info.get("elapsed_sec"),
                "native_metrics": feedback.info.get("benchmark_metrics", {}),
                "metric_keys": feedback.info.get("metric_keys", {})}
