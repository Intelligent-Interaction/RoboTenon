import asyncio
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from robotenon_robocasa365.benchmark import RoboCasa365Benchmark, VIDEO_KEYS
from test_graph_parallel_routing import node, runtime

from robotenon.contracts.context import GraphContext
from robotenon.infra.params import Params
from robotenon.schemas import (
    ActionSpaceSpec,
    BodyExecutionResult,
    BodyExecutionStatus,
    EnvironmentFeedback,
    EnvironmentIO,
)


def test_info_success_terminates_on_the_same_action():
    class FakeEnv:
        calls = 0

        def step(self, action):
            self.calls += 1
            assert set(action) == {
                "action.end_effector_position", "action.end_effector_rotation",
                "action.gripper_close", "action.base_motion", "action.control_mode",
            }
            obs = {VIDEO_KEYS[0]: np.zeros((2, 2, 3), dtype=np.uint8)}
            return obs, 0.0, False, False, {"success": True}

    class Recorder:
        def __init__(self):
            self.steps = []

        def record_video(self, frame):
            assert frame is not None

        def record_step(self, **fields):
            self.steps.append(fields)

    benchmark = RoboCasa365Benchmark()
    benchmark._env = FakeEnv()
    benchmark._recorder = Recorder()
    benchmark._horizon = 100

    result = benchmark.step({"actions": [0.0] * 12})

    assert result.done is True
    assert result.info["success"] is True
    assert benchmark.check_done(result) is True
    assert benchmark.get_step_result(result) == {"success": True}
    assert benchmark._env.calls == 1
    assert benchmark._recorder.steps == [{"reward": 1.0, "done": True, "success": True}]
