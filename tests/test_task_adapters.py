import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from robotenon.infra.params import Params
from robotenon.runtime.config import TaskAdapterConfig, TaskAdaptersConfig
from robotenon.runtime.factory import build_task_adapters
from robotenon.task_adapters import (
    BaseTaskAdapter,
    TaskCase,
    evaluate_task_adapters,
    register_task_adapter,
)


def test_task_adapter_config_preserves_instance_order_and_task_expansion():
    name = f"test-task-adapter-{uuid4().hex}"

    class TestAdapter(BaseTaskAdapter):
        def __init__(self, *, tasks=None, **_kwargs):
            self.tasks = list(tasks or [])

        def enumerate_cases(self, *, adapter_id, params):
            for index, task_id in enumerate(self.tasks):
                yield TaskCase(
                    adapter_id=adapter_id,
                    task_id=task_id,
                    environment_id="test/environment",
                    index=index,
                    parameters={"source": "config"},
                )

    register_task_adapter(name, version="1.0.0", kind="test")(TestAdapter)
    configured = build_task_adapters(
        TaskAdaptersConfig(
            task_adapters=[
                TaskAdapterConfig(id="first", name=name, kind="test", params={"tasks": ["a", "b"]}),
                TaskAdapterConfig(id="second", name=name, kind="test", params={"tasks": ["c"]}),
            ]
        )
    )

    assert [item.instance_id for item in configured] == ["first", "second"]
    assert [case.task_id for case in configured[0].adapter.enumerate_cases(adapter_id="first", params=Params({}))] == ["a", "b"]
    assert [case.task_id for case in configured[1].adapter.enumerate_cases(adapter_id="second", params=Params({}))] == ["c"]


def test_ordered_task_adapter_evaluator_persists_case_status(tmp_path: Path):
    name = f"test-evaluator-adapter-{uuid4().hex}"

    class TestAdapter(BaseTaskAdapter):
        def enumerate_cases(self, *, adapter_id, params):
            yield TaskCase(
                adapter_id=adapter_id,
                task_id="one",
                environment_id="test/environment",
                index=0,
                seed=3,
            )

    register_task_adapter(name, version="1.0.0", kind="test")(TestAdapter)
    configured = build_task_adapters(
        TaskAdaptersConfig(
            task_adapters=[TaskAdapterConfig(id="ordered", name=name, kind="test")]
        )
    )

    async def run_episode(case, output_path, trace_dir, memory_path):
        return {
            "task_success": True,
            "env_steps": 2,
            "episode_json_target": str(output_path),
            "graph_status": "completed",
            "context": {
                "current_graph": "g",
                "history": [{"graph": "g", "node": "done", "event": "exit"}],
                "large_observations": ["kept only in the episode artifact"],
            },
        }

    summary = asyncio.run(
        evaluate_task_adapters(
            config_path=tmp_path / "config.json",
            params=Params({"task": {"seed": 3}}),
            adapters=configured,
            output_dir=tmp_path / "experiments",
            run_id="run-test",
            run_episode=run_episode,
            options={"save_traces": False},
        )
    )

    output_root = tmp_path / "experiments" / "run-test"
    assert summary["num_episodes"] == 1
    assert summary["successes"] == 1
    assert json.loads((output_root / "status.json").read_text())["status"] == "completed"
    episode = json.loads((output_root / "one" / "episode_000_seed_3.json").read_text())
    assert episode["task_success"] is True
    assert episode["result"]["env_steps"] == 2
    assert "context" not in summary["task_adapters"]["ordered"]["episodes"][0]["result"]
    assert summary["task_adapters"]["ordered"]["episodes"][0]["graph_runtime"]["graph_id"] == "g"
    assert "context" in episode["result"]


@pytest.mark.parametrize("returned_failure", [False, True])
def test_task_adapter_evaluator_counts_error_as_attempted_episode(tmp_path: Path, returned_failure):
    name = f"test-evaluator-error-{uuid4().hex}"

    class TestAdapter(BaseTaskAdapter):
        def enumerate_cases(self, *, adapter_id, params):
            yield TaskCase(
                adapter_id=adapter_id,
                task_id="broken",
                environment_id="test/environment",
                index=0,
            )

    register_task_adapter(name, version="1.0.0", kind="test")(TestAdapter)
    configured = build_task_adapters(
        TaskAdaptersConfig(
            task_adapters=[TaskAdapterConfig(id="ordered", name=name, kind="test")]
        )
    )

    async def run_episode(case, output_path, trace_dir, memory_path):
        if returned_failure:
            return {"graph_status": "failed", "error": "invalid model response",
                    "env_steps": 32, "reason": "continue", "task_success": False}
        raise RuntimeError("runner failed before graph execution")

    summary = asyncio.run(
        evaluate_task_adapters(
            config_path=tmp_path / "config.json",
            params=Params({}),
            adapters=configured,
            output_dir=tmp_path / "experiments",
            run_id="run-error",
            run_episode=run_episode,
        )
    )

    assert summary["num_episodes"] == 1
    assert summary["num_completed"] == 0
    assert summary["num_errors"] == 1
    assert summary["successes"] == 0
    assert summary["episode_success_rate"] == 0.0
    if returned_failure:
        episode = summary["task_adapters"]["ordered"]["episodes"][0]
        assert episode["reason"] == "graph_error"
        assert episode["error"] == "invalid model response"
        assert episode["env_steps"] == 32
