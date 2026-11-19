import asyncio

from robotenon.bodies.real_robot_body import RealRobotBody
from robotenon.envs.base import (
    BaseEnvironmentAdapter,
    BaseEnvironmentSession,
    EnvironmentSession,
)
from robotenon.envs.base.fake import FakeEnvironmentAdapter, FakeEnvironmentSession
from robotenon.envs.real_robot import RealRobotAdapter, RealRobotSession
from robotenon.runtime.safety import SafetyStateStore
from robotenon.schemas import (
    Action,
    ActionChunk,
    ActionType,
    EnvironmentCommand,
    SessionSpec,
)


def test_official_environment_adapters_share_base_lifecycle():
    assert issubclass(FakeEnvironmentAdapter, BaseEnvironmentAdapter)
    assert issubclass(FakeEnvironmentSession, BaseEnvironmentSession)
    assert issubclass(RealRobotAdapter, BaseEnvironmentAdapter)
    assert issubclass(RealRobotSession, BaseEnvironmentSession)
    assert not hasattr(EnvironmentSession, "apply_action")
    assert not hasattr(FakeEnvironmentSession, "apply_action")

    async def scenario():
        adapter = FakeEnvironmentAdapter(max_steps=1)
        before_connect = await adapter.health()
        assert before_connect.ready is False
        assert before_connect.message == "not_connected"
        await adapter.connect()
        after_connect = await adapter.health()
        assert after_connect.ready is True
        session = await adapter.open(
            SessionSpec(task_id="demo", environment_id="fake")
        )
        await session.reset(session.spec)
        paused = await session.command(EnvironmentCommand(type="pause"))
        resumed = await session.command(EnvironmentCommand(type="resume"))
        stopped = await session.command(EnvironmentCommand(type="stop"))
        await adapter.close()
        return session, paused, resumed, stopped

    session, paused, resumed, stopped = asyncio.run(scenario())
    assert paused.health.degraded is True
    assert resumed.health.degraded is False
    assert stopped.terminal is True
    assert session.closed is True


def test_real_robot_session_is_observation_only_and_reports_missing_source():
    async def scenario():
        adapter = RealRobotAdapter()
        session = await adapter.open(
            SessionSpec(task_id="demo", environment_id="real_robot")
        )
        feedback = await session.reset(session.spec)
        health = await session.health()
        await adapter.close()
        return feedback, health

    feedback, health = asyncio.run(scenario())
    assert feedback.state == {}
    assert health.ready is False
    assert health.message == "observation_source_not_configured"
    assert not hasattr(RealRobotSession, "apply_action")


def test_real_robot_body_uses_injected_driver_for_actions_and_safe_stop():
    class FakeDriver:
        def __init__(self):
            self.actions = []
            self.stops = []

        async def execute(self, action):
            self.actions.append(action)

        async def hold(self, reason):
            self.stops.append(reason)

    async def scenario():
        driver = FakeDriver()
        body = RealRobotBody(
            driver=driver,
            require_driver=True,
            safety_state=SafetyStateStore(),
        )
        # Action/session identifiers are normally supplied by the runner; a
        # standalone body test only needs the same UUID on the chunk/actions.
        from uuid import uuid4

        session_id = uuid4()
        action = Action(
            session_id=session_id,
            action_type=ActionType.CUSTOM,
            payload={"format": "ee_delta_gripper", "vector": [0.0]},
        )
        result = await body.execute(
            ActionChunk(session_id=session_id, actions=[action], max_duration_ms=1000)
        )
        await body.interrupt("test_safe_stop")
        await body.close()
        return driver, result

    driver, result = asyncio.run(scenario())
    assert result.executed_actions == 1
    assert driver.actions[0].payload["format"] == "ee_delta_gripper"
    assert driver.stops == ["test_safe_stop"]
