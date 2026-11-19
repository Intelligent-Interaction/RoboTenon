import importlib.util
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).parents[1]
IMPL = ROOT / "extensions/benchmarks/models/pi05/implementation.py"


def load_implementation():
    spec = importlib.util.spec_from_file_location("pi05_implementation_test", IMPL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def request(request_id, value=255):
    frame = {
        "images": {
            "agentview": np.full((256, 256, 3), value, dtype=np.uint8),
            "wrist": np.zeros((256, 256, 3), dtype=np.uint8),
        },
        "states": np.arange(8, dtype=np.float32),
    }
    return {
        "request_id": request_id,
        "session_id": request_id,
        "observation": {
            "history": [frame],
            "task_description": f"task {request_id}",
        },
    }


def test_pi05_numpy_batch_matches_lerobot_keys_and_shapes():
    batch = load_implementation().build_numpy_batch([request("a"), request("b", 0)])
    assert batch["observation.images.image"].shape == (2, 3, 256, 256)
    assert batch["observation.images.image2"].shape == (2, 3, 256, 256)
    assert batch["observation.images.image"].dtype == np.float32
    assert batch["observation.images.image"][0].min() == 1.0
    assert batch["observation.state"].shape == (2, 8)
    assert batch["task"] == ["task a", "task b"]


def test_pi05_rejects_missing_wrist_and_bad_history():
    module = load_implementation()
    bad = request("a")
    del bad["observation"]["history"][0]["images"]["wrist"]
    with pytest.raises(ValueError, match="wrist"):
        module.build_numpy_batch([bad])
    bad = request("b")
    bad["observation"]["history"].append(bad["observation"]["history"][0])
    with pytest.raises(ValueError, match="exactly one"):
        module.build_numpy_batch([bad])


def test_libero_can_execute_pi05_continuous_gripper_action():
    from robotenon_libero.benchmark import LIBEROBenchmark

    raw_obs = {
        "agentview_image": np.zeros((256, 256, 3), dtype=np.uint8),
        "robot0_eef_pos": np.zeros(3),
        "robot0_eef_quat": np.array([0.0, 0.0, 0.0, 1.0]),
        "robot0_gripper_qpos": np.zeros(2),
    }

    class Environment:
        robots = []

        def __init__(self):
            self.actions = []

        def step(self, action):
            self.actions.append(action)
            return raw_obs, 0.0, False, {}

    class Recorder:
        def record_video(self, frame):
            pass

        def record_step(self, **fields):
            pass

    benchmark = LIBEROBenchmark(discretize_gripper=False)
    benchmark._env = Environment()
    benchmark._recorder = Recorder()
    benchmark.step({"actions": [0.0] * 6 + [0.25]})
    assert benchmark._env.actions == [[0.0] * 6 + [0.25]]
