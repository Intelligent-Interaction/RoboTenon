import json
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "extensions" / "benchmarks" / "benchmark-support" / "src"))
sys.path.insert(0, str(ROOT / "extensions" / "benchmarks" / "vlabench" / "src"))
sys.path.insert(0, str(ROOT / "extensions" / "benchmarks" / "models" / "xiaomi"))

from preprocessing import (  # noqa: E402
    build_vlabench_messages,
    quaternion_wxyz_to_euler,
    vlabench_observation_to_state,
)
from robotenon_benchmark_support.backend_loader import load, resolve_spec  # noqa: E402
from robotenon_benchmark_support.integration import BenchmarkTasks, task_manifest  # noqa: E402
from robotenon_vlabench._internal.upstream_benchmark import VLABenchBenchmark  # noqa: E402


def test_xiaomi_spec_resolves_native_temporal_contracts():
    backend = ROOT / "extensions" / "benchmarks" / "models" / "xiaomi" / "vla.py"
    _, spec = load(str(backend))

    robocasa = resolve_spec(spec, "robocasa365")
    vlabench = resolve_spec(spec, "vlabench")

    assert robocasa["history"] == {"length": 4, "interval": 2}
    assert robocasa["replan_steps"] == 16
    assert vlabench["history"] == {"length": 1, "interval": 1}
    assert vlabench["replan_steps"] == 5


def test_vlabench_observation_preserves_official_camera_order_and_state():
    benchmark = object.__new__(VLABenchBenchmark)
    benchmark._instruction = "pick the apple"
    cameras = [np.full((2, 3, 3), index, dtype=np.uint8) for index in range(4)]
    ee_state = np.asarray([1.0, 2.0, 3.0, 1.0, 0.0, 0.0, 0.0, 0.04])

    observation = benchmark.make_obs({"rgb": cameras, "ee_state": ee_state}, {"name": "task"})

    assert np.all(observation["images"]["ego"] == 2)
    assert np.all(observation["images"]["base"] == 0)
    assert np.all(observation["images"]["left_wrist"] == 3)
    np.testing.assert_allclose(observation["state"]["ee_state"], ee_state)
    assert observation["task_description"] == "pick the apple"


def test_vlabench_xiaomi_state_and_prompt_match_released_adapter():
    ee_state = np.asarray([1.0, 2.0, 3.0, 1.0, 0.0, 0.0, 0.0, 0.25])
    state = vlabench_observation_to_state({"ee_state": ee_state})
    np.testing.assert_allclose(state, [1.0, 2.4, 2.22, 0.0, 0.0, 0.0, 0.25], atol=1e-6)
    np.testing.assert_allclose(quaternion_wxyz_to_euler([1, 0, 0, 0]), [0, 0, 0])

    images = {
        "ego": np.zeros((4, 5, 3), dtype=np.uint8),
        "base": np.zeros((4, 5, 3), dtype=np.uint8),
        "left_wrist": np.zeros((4, 5, 3), dtype=np.uint8),
    }
    messages = build_vlabench_messages(images, "pick", image_size=8, cot=False)
    image_items = [item for item in messages[0]["content"] if item["type"] == "image"]
    assert [item["image"].size for item in image_items] == [(8, 8)] * 3
    assert messages[-1]["content"][0]["text"] == "<cot></cot>"


def test_vlabench_official_track_enumeration_uses_deterministic_episodes(tmp_path):
    track = "track_1_in_distribution"
    (tmp_path / f"{track}.json").write_text(
        json.dumps({"select_fruit": [{"scene": 1}, {"scene": 2}]})
    )
    benchmark = VLABenchBenchmark(
        tasks=["select_fruit"],
        evaluation_tracks=[track],
        episode_config_dir=str(tmp_path),
    )

    assert benchmark.get_tasks() == [
        {
            "name": f"{track}/select_fruit",
            "task_name": "select_fruit",
            "suite": track,
            "evaluation_track": track,
            "episode_config_path": str(tmp_path / f"{track}.json"),
            "episode_count": 2,
            "episode_max_steps": 200,
        }
    ]
    assert benchmark.get_metadata()["max_episodes_per_task"] == 2
    assert benchmark.get_metric_keys() == {
        "success": "mean",
        "intention_score": "mean",
        "progress_score": "mean",
    }


def test_vlabench_case_enumeration_honors_each_tracks_episode_count(tmp_path):
    track = "track_2_cross_category"
    (tmp_path / f"{track}.json").write_text(
        json.dumps(
            {
                "select_fruit": [{"scene": index} for index in range(50)],
                "insert_flower": [{"scene": index} for index in range(10)],
            }
        )
    )
    benchmark = VLABenchBenchmark(
        evaluation_tracks=[track], episode_config_dir=str(tmp_path)
    )
    manifest = task_manifest(benchmark)
    adapter = BenchmarkTasks(manifest=manifest, episodes_per_task=50)

    cases = list(adapter._ordered_cases(adapter_id="vlabench_benchmark", params={}))

    assert len(cases) == 60
    assert [case.index for case in cases] == list(range(60))
    flower_cases = [case for case in cases if case.task_id.endswith("insert_flower")]
    assert [case.episode for case in flower_cases] == list(range(10))
    assert all(
        "episode_count"
        not in case.parameters["environment_parameters"]["native_task"]
        for case in cases
    )


def test_vlabench_uses_official_per_task_horizons():
    benchmark = VLABenchBenchmark(
        tasks=["select_fruit", "select_poker", "select_painting", "add_condiment"]
    )

    manifest = task_manifest(benchmark)
    assert [record["episode_max_steps"] for record in manifest["tasks"]] == [
        200,
        100,
        100,
        300,
    ]
    assert manifest["metadata"]["max_steps"] == 300

    adapter = BenchmarkTasks(manifest=manifest, episodes_per_task=1)
    cases = list(adapter._ordered_cases(adapter_id="vlabench_benchmark", params={}))
    assert [case.horizon for case in cases] == [200, 100, 100, 300]
    assert [
        case.parameters["environment_parameters"]["native_max_steps"]
        for case in cases
    ] == [200, 100, 100, 300]
    assert all(
        "episode_max_steps"
        not in case.parameters["environment_parameters"]["native_task"]
        for case in cases
    )


def test_explicit_max_steps_overrides_vlabench_task_horizons():
    benchmark = VLABenchBenchmark(
        tasks=["select_poker", "add_condiment"], max_steps=42
    )
    manifest = task_manifest(benchmark)
    adapter = BenchmarkTasks(manifest=manifest, episodes_per_task=1)

    cases = list(adapter._ordered_cases(adapter_id="vlabench_benchmark", params={}))

    assert [case.horizon for case in cases] == [42, 42]


def test_task_horizon_extension_is_backward_compatible():
    manifest = {
        "tasks": [
            {
                "index": 0,
                "name": "legacy",
                "fields": {"name": "legacy"},
                "rehydrate": False,
            }
        ],
        "metadata": {"max_steps": 275},
        "metric_keys": {"success": "mean"},
    }
    adapter = BenchmarkTasks(manifest=manifest, episodes_per_task=1)

    case = next(adapter._ordered_cases(adapter_id="legacy_benchmark", params={}))

    assert case.horizon == 275
    assert case.parameters["environment_parameters"]["native_max_steps"] == 275
