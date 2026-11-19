"""Bounded audit probes. Passing means the documented defect exists.

Run from RoboClaw: python -m pytest -o addopts= -q docs/audit-2026-09-20/reproduce.py
Uses only fake drivers; never connects to hardware or model services.
"""
import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

from robosys.bodies.real_robot_body import RealRobotBody
from robosys.modules.verify import VerifyModule
from robosys.runtime.safety import SafetyStateStore
from robosys.schemas import Action, ActionChunk
from robosys.services.body import InProcessBodyService


def test_sync_real_driver_blocks_loop_and_bypasses_timeout():
    async def probe():
        events = []

        class Driver:
            def execute(self, action):
                events.append("driver_started")
                time.sleep(0.12)
                events.append("driver_finished")

            def hold(self, reason):
                pass

        body = RealRobotBody(driver=Driver(), safety_state=SafetyStateStore())
        session = uuid4()

        async def tick():
            await asyncio.sleep(0.02)
            events.append("tick")

        timer = asyncio.create_task(tick())
        try:
            result = await body.execute(ActionChunk(
                session_id=session,
                actions=[Action(session_id=session, timeout_ms=25)],
                max_duration_ms=2000,
            ))
            await timer
            assert events.index("tick") > events.index("driver_finished")
            assert result.action_results[0].status == "succeeded"
            print({"events": events, "ack": result.action_results[0].status})
        finally:
            await body.close()

    asyncio.run(probe())


def test_verifier_string_false_is_committed_as_success():
    async def probe():
        class Params:
            def get(self, key, contract):
                return {"model.hlp_tool": "fake", "task.task_id": "pick"}[key]

        async def call_tool(*args):
            return SimpleNamespace(ok=True, output={"subtask_success": "false"}, metadata={})

        ctx = SimpleNamespace(
            feedback=None, values={"needs_plan": True, "instruction": "pick"},
            params=Params(), call_tool=call_tool,
        )
        await VerifyModule().run(ctx)
        assert ctx.values["subtask_success"] is True
        assert "pick" in ctx.values["stm"]["completed"]
        print({"input": "false", "committed_success": ctx.values["subtask_success"]})

    asyncio.run(probe())


def test_same_key_different_payload_returns_previous_success():
    async def probe():
        executed = []

        async def execute(action):
            executed.append(action.payload)

        body = InProcessBodyService(execute, safety_state=SafetyStateStore())
        session = uuid4()
        try:
            for value in [1, 2]:
                result = await body.execute(ActionChunk(session_id=session, actions=[
                    Action(session_id=session, idempotency_key="same", payload={"target": value})
                ]))
                assert result.action_results[0].status == "succeeded"
            assert executed == [{"target": 1}]
            print({"executed": executed, "second_executed_actions": result.executed_actions})
        finally:
            await body.close()

    asyncio.run(probe())
