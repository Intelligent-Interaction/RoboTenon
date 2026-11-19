import asyncio
from types import SimpleNamespace

import pytest

from robotenon.infra.params import Params
from robotenon.modules.plan import PlanModule


@pytest.mark.parametrize("require_success,success,decision", [
    (True, None, "act"), (True, False, "act"),
    (True, True, "stop"), (False, None, "stop"),
])
def test_planner_stop_respects_environment_authority(require_success, success, decision):
    async def call_tool(*args):
        return SimpleNamespace(ok=True, metadata={}, output={
            "decision": "stop", "subgoal": "Close the cabinet.",
            "remaining": [], "completed": ["Close the cabinet."],
        })

    ctx = SimpleNamespace(
        params=Params({"model": {"hlp_tool": "planner"},
                       "task": {"task_id": "CloseCabinet"},
                       "policy": {"planner_stop_requires_environment_success": require_success}}),
        feedback=SimpleNamespace(state={}, metadata={"instruction": "Close the cabinet."}, success=success),
        values={"reason": "continue", "stm": {"recent_verifier": {"subtask_success": True}}},
        call_tool=call_tool,
    )
    ctx.inputs = dict(ctx.values)
    asyncio.run(PlanModule().run(ctx))
    assert ctx.values["planner_decision"] == decision
    if decision == "act":
        assert ctx.values["stm"]["remaining"] == ["Close the cabinet."]
        assert ctx.values["instruction"] == "Close the cabinet."
        assert not ctx.values["hlp_success"]
    else:
        assert ctx.values["reason"] == "planner_stop"
