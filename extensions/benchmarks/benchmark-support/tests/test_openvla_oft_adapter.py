"""CPU contracts only: no model weights, Torch, TF, MuJoCo or GPU."""

import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from robotenon_benchmark_support.backend_loader import load, observation_params, validate_spec
from robotenon_benchmark_support.configure import application_config


ROOT = Path(__file__).resolve().parents[4]
BACKEND = ROOT / "extensions/benchmarks/models/openvla-oft/vla.py"
PROFILE = ROOT / "extensions/benchmarks/libero/configs/openvla_oft_spatial.json"


@pytest.fixture
def adapter_module():
    # Import only the NumPy adapter implementation, never call create/_load.
    factory, _ = load(str(BACKEND))
    import importlib

    return importlib.import_module(factory.__module__.rpartition(".")[0] + ".implementation")


@pytest.fixture
def observation():
    image = np.zeros((256, 256, 3), dtype=np.uint8)
    image[0, 0] = [1, 2, 3]
    return {"history": [{"images": {"agentview": image, "wrist": image.copy()},
                         "states": np.arange(8, dtype=np.float64)}],
            "task_description": "Pick up the bowl"}


def test_metadata_and_profile_are_lightweight():
    script = f"""
import sys
sys.path.insert(0, {str(ROOT / 'extensions/benchmarks/benchmark-support/src')!r})
from robotenon_benchmark_support.backend_loader import load, observation_params
load({str(BACKEND)!r})
observation_params({str(BACKEND)!r})
assert not ({{'torch', 'tensorflow', 'libero', 'mujoco', 'prismatic'}} & sys.modules.keys())
"""
    subprocess.run([sys.executable, "-c", script], check=True)
    _, spec = load(str(BACKEND))
    validate_spec(spec, "libero", 1)
    with pytest.raises(ValueError, match="batch_size=1"):
        validate_spec(spec, "libero", 2)
    profile = json.loads(PROFILE.read_text())
    config = application_config(profile, policy_factory="example:create")
    for key, value in observation_params(str(BACKEND)).items():
        assert profile["params"][key] == value
    assert config["task_adapters"][0]["params"]["params"]["env_seed"] == 0


def test_observation_ownership(adapter_module, observation):
    prepared, instruction = adapter_module.prepare_observation(observation)
    assert instruction == "Pick up the bowl"
    np.testing.assert_array_equal(prepared["full_image"][0, 0], [1, 2, 3])
    assert not np.shares_memory(prepared["state"], observation["history"][0]["states"])
    prepared["state"][:] = 0
    assert observation["history"][0]["states"][-1] == 7


@pytest.mark.parametrize("field", ["wrist", "state", "instruction", "history"])
def test_bad_observations_fail_before_prediction(adapter_module, observation, field):
    if field == "wrist":
        del observation["history"][0]["images"]["wrist"]
    elif field == "state":
        observation["history"][0]["states"][0] = np.nan
    elif field == "instruction":
        observation["task_description"] = ""
    else:
        observation["history"] *= 2
    with pytest.raises(ValueError):
        adapter_module.prepare_observation(observation)


def test_gripper_mapping_keeps_halfway_zero(adapter_module):
    raw = np.zeros((8, 7), dtype=np.float32)
    raw[:, 0] = np.arange(8)
    raw[:, -1] = [0, 0.25, 0.5, 0.75, 1, 0.5, 0, 1]
    before = raw.copy()
    converted = np.array([item["actions"] for item in adapter_module.native_actions(raw)])
    np.testing.assert_array_equal(converted[:, -1], [1, 1, 0, -1, -1, 0, 1, -1])
    np.testing.assert_array_equal(converted[:, :6], raw[:, :6])
    np.testing.assert_array_equal(raw, before)
    raw[0, 0] = np.inf
    with pytest.raises(ValueError):
        adapter_module.native_actions(raw)
    with pytest.raises(ValueError):
        adapter_module.native_actions(np.zeros((7, 7)))


def test_session_request_result_and_no_hidden_retry(adapter_module, observation, monkeypatch):
    adapter = adapter_module.OpenVLAOFT.__new__(adapter_module.OpenVLAOFT)
    adapter.sessions = set()
    metadata = {"benchmark_id": "libero_benchmark", "task": {"suite": "libero_spatial"}}
    with pytest.raises(ValueError):
        adapter.open_session("bad", {**metadata, "task": {"suite": "libero_object"}})
    adapter.open_session("episode", metadata)
    request = {"request_id": "request-17", "session_id": "episode", "observation": observation}
    calls = []

    def predict(obs, instruction):
        calls.append(instruction)
        return np.zeros((8, 7))

    monkeypatch.setattr(adapter, "_predict", predict)
    result = adapter.act_batch([request])
    assert result[0]["request_id"] == "request-17"
    assert len(result[0]["action"]["native_actions"]) == 8
    assert len(calls) == 1
    with pytest.raises(ValueError):
        adapter.act_batch([request, request])

    def fail(*args):
        calls.append("error")
        raise RuntimeError("prediction failed")

    monkeypatch.setattr(adapter, "_predict", fail)
    with pytest.raises(RuntimeError, match="prediction failed"):
        adapter.act_batch([request])
    assert calls.count("error") == 1
    adapter.close_session("episode", {})
    with pytest.raises(ValueError):
        adapter.act_batch([request])
    adapter.close()


def test_libero_boundary_seed_wait_images_state_and_gripper(adapter_module, monkeypatch):
    """Exercise real benchmark methods with an in-memory simulator stand-in."""
    monkeypatch.syspath_prepend(str(ROOT / "extensions/benchmarks/libero/src"))
    from robotenon_libero.benchmark import LIBEROBenchmark
    from robotenon_benchmark_support.recording import EpisodeRecorder

    image = np.zeros((256, 256, 3), dtype=np.uint8)
    image[0, 0] = [10, 20, 30]
    raw = {"agentview_image": image, "robot0_eye_in_hand_image": image.copy(),
           "robot0_eef_pos": np.array([1., 2., 3.]),
           "robot0_eef_quat": np.array([0., 0.6, 0., -0.8]),
           "robot0_gripper_qpos": np.array([0.01, 0.02])}

    class Sim:
        def __init__(self, **kwargs):
            self.actions = []
            self.robots = [SimpleNamespace(controller=SimpleNamespace(
                ee_pos=np.array([1., 2., 3.]), ee_ori_mat=np.eye(3)))]

        def seed(self, seed):
            self.seed_value = seed

        def reset(self):
            pass

        def set_init_state(self, state):
            self.initial_state = state
            return raw

        def step(self, action):
            self.actions.append(action)
            return raw, 0., False, {}

    libero = ModuleType("libero")
    package = ModuleType("libero.libero")
    package.get_libero_path = lambda key: "/unused-bddl"
    envs = ModuleType("libero.libero.envs")
    envs.OffScreenRenderEnv = Sim
    for name, module in (("libero", libero), ("libero.libero", package), ("libero.libero.envs", envs)):
        monkeypatch.setitem(sys.modules, name, module)
    benchmark = LIBEROBenchmark(**json.loads(PROFILE.read_text())["params"])
    benchmark._task_suite = SimpleNamespace(get_task_init_states=lambda task_id: ["init-0", "init-1"])
    task = {"name": "Pick up the bowl", "task_id": 0, "episode_idx": 1,
            "task_obj": SimpleNamespace(problem_folder="spatial", bddl_file="task.bddl")}
    benchmark._recorder = EpisodeRecorder()
    obs = benchmark.make_obs(benchmark.reset(task), task)
    assert benchmark._env.seed_value == 0
    assert benchmark._env.initial_state == "init-1"
    assert len(benchmark._env.actions) == 10
    assert all(action == [0., 0., 0., 0., 0., 0., -1.] for action in benchmark._env.actions)
    np.testing.assert_array_equal(obs["images"]["agentview"][-1, -1], [10, 20, 30])
    np.testing.assert_array_equal(obs["images"]["wrist"][-1, -1], [10, 20, 30])
    assert obs["states"].shape == (8,)
    assert obs["states"][4] > np.pi  # no quaternion antipodal normalization
    prepared, _ = adapter_module.prepare_observation({"history": [obs], "task_description": task["name"]})
    np.testing.assert_array_equal(prepared["full_image"], obs["images"]["agentview"])
    action = adapter_module.native_actions(np.full((8, 7), 0.5))[0]
    result = benchmark.step(action)
    assert benchmark._env.actions[-1][-1] == 0  # no second gripper binarization
    assert benchmark.get_metadata()["max_steps"] == 220
