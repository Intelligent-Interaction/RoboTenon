"""Graph-side request shaping for the configured planner Tool."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..runtime.registry import register_module
from .base import BaseModule


def _string_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if value in (None, False, ""):
        return list(default)
    return [str(value)]


@register_module(
    "plan",
    version="1.0.0",
    kind="planner",
    description="Ask the configured planner Tool for the next object-level subgoal.",
)
class PlanModule(BaseModule):
    def __init__(self) -> None:
        pass

    async def run(self, ctx: Any) -> dict[str, Any]:
        feedback = getattr(ctx, "feedback", None)
        from ..contracts.workspace import thaw
        values = thaw(ctx.inputs)
        params = getattr(ctx, "params", None)
        if params is None:
            raise RuntimeError("plan requires the shared Params object in ctx.params")
        tool_name = params.get("model.hlp_tool", [str, None])
        task_id = params.get("task.task_id", [str, None])
        if not tool_name:
            raise ValueError("missing required parameter: model.hlp_tool")
        if not task_id:
            raise ValueError("missing required parameter: task.task_id")
        state = dict(feedback.state or {}) if feedback is not None else {}
        task = ""
        if feedback is not None:
            task = str(
                state.get("annotation.human.task_description")
                or (feedback.metadata or {}).get("instruction", "")
                or ""
            )
        task = task or str(task_id)
        stm_value = values.get("stm")
        if isinstance(stm_value, Mapping):
            stm = {
                "goal": str(stm_value.get("goal") or task),
                "completed": _string_list(stm_value.get("completed"), []),
                "remaining": _string_list(stm_value.get("remaining"), [task]),
                "physical_belief": dict(stm_value.get("physical_belief") or {}),
                "recent_verifier": dict(stm_value.get("recent_verifier") or {}),
                "recent_actions": list(stm_value.get("recent_actions") or [])[-4:],
            }
        else:
            stm = {
                "goal": task,
                "completed": [],
                "remaining": [task],
                "physical_belief": {},
                "recent_verifier": {},
                "recent_actions": [],
            }
        retrieved = values.get("ltm_records", [])
        if not isinstance(retrieved, list):
            retrieved = []
        schema = {
            "decision": "act|stop",
            "subgoal": "one object-level manipulation outcome",
            "completed": ["subtasks"],
            "remaining": ["subtasks"],
            "reason": "brief reason",
        }
        prompt = (
            "Decompose and update the robot task, then choose exactly one next "
            "object-level subgoal. Do not emit action vectors or named robot "
            "functions. Do not split a manipulation into approach/grasp/lift/release "
            "micro-steps. Keep the same subgoal until the verifier confirms it. "
            "The subgoal is passed verbatim to a pretrained low-level VLA: "
            "preserve the original task's action and object wording, and for a "
            "single-step task prefer the original task sentence over synonyms or "
            "preparatory verbs such as approach, align, or push. "
            "Choose stop only when every part of the original task is complete. "
            "Return JSON only.\n"
            f"Task: {task}\n"
            f"Short-term memory: {json.dumps(stm, ensure_ascii=False)}\n"
            f"Retrieved long-term memory: {json.dumps(retrieved, ensure_ascii=False)}\n"
            f"Schema: {json.dumps(schema)}"
        )
        result = await ctx.call_tool(
            tool_name,
            {
                "task": task,
                "prompt": prompt,
                "system_prompt": (
                    "You are the high-level orchestrator of an embodied robot. "
                    "Return JSON only."
                ),
                "image_roles": ["external", "wrist"],
            },
        )
        if not result.ok:
            raise RuntimeError(f"{tool_name} Tool failed: {result.error or 'unknown error'}")
        output = result.output if isinstance(result.output, dict) else {}
        decision = str(output.get("decision", "act")).lower()
        if decision not in {"act", "stop"}:
            decision = "act"
        subgoal = str(output.get("subgoal") or task)
        completed = _string_list(output.get("completed"), stm["completed"])
        remaining = _string_list(output.get("remaining"), stm["remaining"] or [task])
        normalized = {
            "decision": decision,
            "subgoal": subgoal,
            "completed": completed,
            "remaining": remaining,
            "reason": str(output.get("reason", "")),
        }
        # A stop decision is only admissible after a positive verifier result
        # and an empty remaining list, matching the archive harness.
        recent_verifier = stm.get("recent_verifier", {})
        if decision == "stop" and not (
            bool(recent_verifier.get("subtask_success")) and not remaining
        ):
            normalized["decision"] = "act"
            normalized["subgoal"] = subgoal or task
        # Benchmark environments have an authoritative success predicate.
        # A visual verifier's false positive must not end their rollout.
        if (
            normalized["decision"] == "stop"
            and params.get("policy.planner_stop_requires_environment_success", [bool, None])
            and getattr(feedback, "success", None) is not True
        ):
            normalized["decision"] = "act"
            normalized["subgoal"] = task
            normalized["remaining"] = remaining = [task]
            normalized["reason"] = "Environment has not confirmed task success; continue the original task."
        stm.update(
            {
                "goal": task,
                "completed": completed,
                "remaining": remaining,
            }
        )
        values = {}
        values["stm"] = stm
        values["instruction"] = normalized["subgoal"]
        values["planner_decision"] = normalized["decision"]
        values["planner_result"] = normalized
        values["hlp_success"] = bool(
            normalized["decision"] == "stop"
            and bool(recent_verifier.get("subtask_success"))
            and not remaining
        )
        if normalized["decision"] == "stop":
            values["reason"] = "planner_stop"
        ctx.values.update(values)
        return {
            **values,
            "plan": normalized,
            "instruction": normalized["subgoal"],
            "planner": result.metadata,
            "stm": stm,
        }


__all__ = ["PlanModule"]
