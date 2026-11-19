"""Post-action visual verifier for embodied task loops."""

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
    "verify",
    version="1.0.0",
    kind="evaluator",
    description="Use Qwen3.5-VL to verify the executed object-level subgoal and update STM.",
)
class VerifyModule(BaseModule):
    """Mirror the archive verifier while keeping the model in a Tool."""

    def __init__(self) -> None:
        pass

    async def run(self, ctx: Any) -> dict[str, Any]:
        feedback = getattr(ctx, "feedback", None)
        from ..contracts.workspace import thaw
        values = thaw(ctx.inputs)
        params = getattr(ctx, "params", None)
        if params is None:
            raise RuntimeError("verify requires the shared Params object in ctx.params")
        tool_name = params.get("model.hlp_tool", [str, None])
        if not tool_name:
            raise ValueError("missing required parameter: model.hlp_tool")
        task_id = params.get("task.task_id", [str, None])
        if not task_id:
            raise ValueError("missing required parameter: task.task_id")
        # If the configured action chunk is smaller than the VLA output, keep
        # consuming the resident queue before verifying. This preserves the
        # configurable ``policy.action_chunk_size`` contract; with the
        # archive-aligned value 16 this branch is not taken.
        if not bool(values.get("needs_plan", True)):
            return {
                "stm": values.get("stm", {}),
                "needs_plan": False,
                "verification": None,
                "skipped": True,
                "reason": "action_queue_remaining",
                "action_queue_remaining": int(values.get("action_queue_remaining", 0) or 0),
            }
        state = dict(feedback.state or {}) if feedback is not None else {}
        task = str(
            state.get("annotation.human.task_description")
            or task_id
        )
        subgoal = str(
            values.get("instruction")
            or (values.get("planner_result") or {}).get("subgoal")
            or task
        )
        prompt = (
            f"Original task: {task}\n"
            f"Current subtask: {subgoal}\n"
            "Use the supplied current camera views and their labels. "
            "Judge whether the current subtask is visibly "
            "complete. An attempted action or proximity is not completion. "
            "Return JSON only with exactly these keys:\n"
            '{"subtask_success": false, "physical_belief_update": {}}'
        )
        result = await ctx.call_tool(
            tool_name,
            {
                "task": task,
                "prompt": prompt,
                "system_prompt": (
                    "Use the supplied current images to verify the subtask. "
                    "Return exactly one JSON object."
                ),
                "image_roles": ["external", "wrist"],
            },
        )
        if not result.ok:
            raise RuntimeError(
                f"{tool_name} verifier failed: {result.error or 'unknown error'}"
            )
        output = result.output
        if not isinstance(output, Mapping) or type(output.get("subtask_success")) is not bool:
            raise ValueError(f"{tool_name} verifier output requires a JSON boolean subtask_success")
        belief = output.get("physical_belief_update", {})
        if not isinstance(belief, Mapping):
            raise ValueError(f"{tool_name} verifier physical_belief_update must be an object")
        verification = {
            "subtask_success": output["subtask_success"],
            "physical_belief_update": dict(belief),
        }

        stm_value = values.get("stm")
        stm = dict(stm_value) if isinstance(stm_value, Mapping) else {
            "goal": task,
            "completed": [],
            "remaining": [task],
            "physical_belief": {},
            "recent_verifier": {},
            "recent_actions": [],
        }
        stm["goal"] = task
        stm["physical_belief"] = {
            **dict(stm.get("physical_belief") or {}),
            **verification["physical_belief_update"],
        }
        stm["recent_verifier"] = verification
        completed = _string_list(stm.get("completed"), [])
        remaining = _string_list(stm.get("remaining"), [task])
        if verification["subtask_success"]:
            if subgoal not in completed:
                completed.append(subgoal)
            remaining = [item for item in remaining if item != subgoal]
        stm["completed"] = completed
        stm["remaining"] = remaining
        actions = getattr(ctx, "body_result", None)
        action_count = int(getattr(actions, "executed_actions", 0) or 0)
        recent_actions = list(stm.get("recent_actions") or [])
        recent_actions.append(
            {
                "subgoal": subgoal,
                "action_count": action_count,
                "subtask_success": verification["subtask_success"],
            }
        )
        stm["recent_actions"] = recent_actions[-4:]

        values = {}
        values["stm"] = stm
        values["verifier_result"] = verification
        values["subtask_success"] = verification["subtask_success"]
        # A completed chunk must be followed by an orchestrator decision. The
        # action module will refill the queue only after that decision.
        values["needs_plan"] = True
        ctx.values.update(values)
        return {
            **values,
            "verification": verification,
            "stm": stm,
            "verifier": result.metadata,
            "prompt": json.dumps(prompt, ensure_ascii=False),
        }


__all__ = ["VerifyModule"]
