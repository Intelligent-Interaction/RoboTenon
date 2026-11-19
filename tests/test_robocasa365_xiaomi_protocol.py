import json
import sys
from types import ModuleType

import numpy as np
import pytest

from robotenon_robocasa365 import benchmark as benchmark_module
from robotenon_robocasa365._internal.batching import RNG_MODES, XiaomiBatchVLA
from robotenon_robocasa365._internal.xiaomi_protocol import (
    observation_to_state,
    quat_xyzw_to_axis_angle,
    sample_history,
)
from robotenon_robocasa365.benchmark import RoboCasa365Benchmark, VIDEO_KEYS


TARGET50 = ["AtomicA", "AtomicB", "CompositeSeen", "CompositeUnseen"]
REGISTRY = {
    "target50": TARGET50,
    "atomic_seen": TARGET50[:2],
    "composite_seen": TARGET50[2:3],
    "composite_unseen": TARGET50[3:],
}


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(benchmark_module, "_task_registry", lambda: REGISTRY)
    monkeypatch.setattr(
        benchmark_module,
        "_task_horizon",
        lambda task: {name: 450 + index * 150 for index, name in enumerate(TARGET50)}[
            task
        ],
    )


def test_xiaomi_profile_is_exact_and_has_no_global_horizon():
    path = (
        benchmark_module.__file__
        and __import__("pathlib").Path(benchmark_module.__file__).parents[2]
        / "configs/xiaomi_official.json"
    )
    profile = json.loads(path.read_text())
    assert profile == {
        "benchmark_id": "robocasa365_benchmark",
        "episodes_per_task": 50,
        "params": {
            "split": "pretrain",
            "seed": 7,
            "camera_size": 256,
            "episode_seed_stride": 50,
        },
    }
    assert "max_steps" not in profile
    assert "success_check_interval" not in profile["params"]


def test_target50_order_indices_and_original_subset_indices(registry):
    benchmark = RoboCasa365Benchmark()
    assert benchmark.get_tasks() == [
        {
            "name": name,
            "suite": (
                "atomic_seen"
                if index < 2
                else "composite_seen"
                if index == 2
                else "composite_unseen"
            ),
            "target50_index": index,
        }
        for index, name in enumerate(TARGET50)
    ]
    subset = RoboCasa365Benchmark(tasks=["CompositeUnseen", "AtomicB"])
    assert [task["target50_index"] for task in subset.get_tasks()] == [3, 1]


@pytest.mark.parametrize(
    ("task_index", "episode", "expected"),
    [(0, 0, 7), (0, 49, 56), (1, 0, 57), (3, 49, 206)],
)
def test_xiaomi_episode_seed_formula(registry, task_index, episode, expected):
    class Environment:
        def __init__(self):
            self.seeds = []

        def reset(self, *, seed):
            self.seeds.append(seed)
            return {VIDEO_KEYS[0]: np.zeros((2, 2, 3), dtype=np.uint8)}, {}

    class Recorder:
        def record_video(self, frame):
            assert frame is not None

    benchmark = RoboCasa365Benchmark(seed=7, episode_seed_stride=50)
    benchmark._env = Environment()
    benchmark._current_task = TARGET50[task_index]
    benchmark._recorder = Recorder()
    task = benchmark.get_tasks()[task_index]
    benchmark.reset({**task, "episode_idx": episode})
    assert benchmark._env.seeds == [expected]
    assert benchmark._horizon == 450 + task_index * 150


def test_environment_constructor_uses_official_base_seed(monkeypatch, registry):
    from robotenon_robocasa365._internal import rendering

    monkeypatch.setattr(rendering, "install_egl_cleanup_guard", lambda: None)
    calls = []
    gymnasium = ModuleType("gymnasium")
    gymnasium.make = lambda name, **kwargs: calls.append((name, kwargs)) or object()
    wrapper = ModuleType("robocasa.wrappers.gym_wrapper")
    wrappers = ModuleType("robocasa.wrappers")
    monkeypatch.setitem(sys.modules, "gymnasium", gymnasium)
    monkeypatch.setitem(sys.modules, "robocasa", ModuleType("robocasa"))
    monkeypatch.setitem(sys.modules, "robocasa.wrappers", wrappers)
    monkeypatch.setitem(sys.modules, "robocasa.wrappers.gym_wrapper", wrapper)

    benchmark = RoboCasa365Benchmark(seed=7, camera_size=256)
    benchmark._make_env("AtomicA")
    assert calls == [
        (
            "robocasa/AtomicA",
            {
                "split": "pretrain",
                "seed": 7,
                "enable_render": True,
                "camera_widths": 256,
                "camera_heights": 256,
                "disable_env_checker": True,
            },
        )
    ]


def test_rng_modes_are_explicit_and_invalid_stride_is_rejected():
    assert RNG_MODES == {"official_stream", "session_deterministic"}
    for value in (-1, 1.5, True):
        with pytest.raises(ValueError, match="episode_seed_stride"):
            RoboCasa365Benchmark(episode_seed_stride=value)


def test_xiaomi_loader_casts_rotary_buffers_like_official_server(monkeypatch):
    torch = pytest.importorskip("torch")
    from types import SimpleNamespace

    class TinyCheckpoint(torch.nn.Module):
        def __init__(self):
            super().__init__()
            # HF loading already casts parameters, but not these buffers.
            self.weight = torch.nn.Parameter(torch.ones(1, dtype=torch.bfloat16))
            self.register_buffer("inv_freq", torch.tensor([0.1234567]))

        def cuda(self):
            return self  # Exercise the loader without requiring a GPU.

    model = TinyCheckpoint()
    transformers = ModuleType("transformers")
    transformers.AutoModel = SimpleNamespace(from_pretrained=lambda *a, **kw: model)
    transformers.AutoProcessor = SimpleNamespace(
        from_pretrained=lambda *a, **kw: SimpleNamespace(tokenizer=SimpleNamespace())
    )
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    policy = XiaomiBatchVLA("tiny-checkpoint", rng_mode="official_stream")
    assert policy.model.inv_freq.dtype == torch.bfloat16
    assert policy.model.inv_freq.float().item() == torch.tensor(
        [0.1234567], dtype=torch.bfloat16
    ).float().item()


@pytest.mark.parametrize("fails", [False, True])
def test_old_renderer_cleanup_preserves_live_context(fails):
    from types import SimpleNamespace
    from robotenon_robocasa365._internal.rendering import _guard_finalizer

    live, old = SimpleNamespace(address=2), SimpleNamespace(address=1)
    current = [live]
    freed = []
    restored = []

    def bind(display, draw, read, context):
        restored.append((display, draw, read, context))
        current[0] = context

    egl = SimpleNamespace(
        EGL_DRAW=1, EGL_READ=2,
        eglGetCurrentContext=lambda: current[0],
        eglGetCurrentDisplay=lambda: "display",
        eglGetCurrentSurface=lambda side: f"surface-{side}",
        eglMakeCurrent=bind,
    )
    renderer = SimpleNamespace(gl_ctx=SimpleNamespace(
        _context=old, make_current=lambda: current.__setitem__(0, old)
    ))

    def destructor(renderer):
        freed.append(current[0].address)
        current[0] = None  # upstream eglReleaseThread
        if fails:
            raise RuntimeError("cleanup failed")

    guarded = _guard_finalizer(destructor, egl)
    if fails:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            guarded(renderer)
    else:
        guarded(renderer)
    assert freed == [old.address]
    assert current[0] is live
    assert restored == [("display", "surface-1", "surface-2", live)]


@pytest.mark.parametrize("has_current", [False, True])
def test_renderer_cleanup_does_not_restore_a_destroyed_context(has_current):
    from types import SimpleNamespace
    from robotenon_robocasa365._internal.rendering import _guard_finalizer

    owner = SimpleNamespace(address=1)
    current = [owner if has_current else None]
    calls = []
    egl = SimpleNamespace(
        EGL_DRAW=1, EGL_READ=2,
        eglGetCurrentContext=lambda: current[0],
        eglGetCurrentDisplay=lambda: "display",
        eglGetCurrentSurface=lambda side: None,
        eglMakeCurrent=lambda *args: calls.append(args),
    )
    renderer = SimpleNamespace(gl_ctx=SimpleNamespace(
        _context=owner, make_current=lambda: current.__setitem__(0, owner)
    ))

    def destructor(renderer):
        assert current[0] is owner
        current[0] = None

    _guard_finalizer(destructor, egl)(renderer)
    assert current[0] is None
    assert calls == []


def test_batch_provider_is_stateless_except_query_rng():
    provider = XiaomiBatchVLA.__new__(XiaomiBatchVLA)
    provider.sessions = {"episode": {"query": 0, "seed": None, "benchmark": "robocasa365"}}
    provider._infer = lambda requests: np.zeros((len(requests), 2, 12))
    requests = [
        {
            "session_id": "episode",
            "request_id": "request",
            "observation": {"history": [object()]},
        }
    ]
    response = provider.act_batch(requests)
    assert len(response[0]["action"]["native_actions"]) == 2
    assert set(provider.sessions["episode"]) == {"query", "seed", "benchmark"}
    assert all("control_mode" not in action for action in response[0]["action"]["native_actions"])


def test_batch_provider_rejects_unassembled_history():
    provider = XiaomiBatchVLA.__new__(XiaomiBatchVLA)
    provider.sessions = {}
    with pytest.raises(ValueError, match="require an observation history"):
        provider.act_batch(
            [{"session_id": "episode", "request_id": "request", "observation": {}}]
        )


def test_xiaomi_state_layout_and_history_sampling_match_release():
    observation = {
        "state.end_effector_position_relative": [1.0, 2.0, 3.0],
        "state.end_effector_rotation_relative": [0.0, 0.0, 0.0, 1.0],
        "state.gripper_qpos": [0.25, -0.25],
        "state.base_position": [4.0, 5.0, 6.0],
        "state.base_rotation": [0.0, 0.0, 0.0, 1.0],
    }
    state = observation_to_state(observation)
    assert state.dtype == np.float32
    assert state.shape == (14,)
    np.testing.assert_array_equal(
        state,
        [1, 2, 3, 0, 0, 0, 0.25, -0.25, 4, 5, 6, 0, 0, 0],
    )
    np.testing.assert_array_equal(
        quat_xyzw_to_axis_angle([0, 0, 0, -1]), np.zeros(3, dtype=np.float32)
    )

    from collections import deque

    history = deque(np.asarray([index], dtype=np.float32) for index in range(7))
    np.testing.assert_array_equal(
        sample_history(history, length=4, interval=2).reshape(-1), [0, 2, 4, 6]
    )
