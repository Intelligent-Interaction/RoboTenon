import asyncio
import json
from pathlib import Path

import pytest

from robotenon.infra.trace_query import TraceQuery
from robotenon.runtime.experiment import ExperimentRequest, run_experiment
from robotenon.runtime.runner import EpisodeRequest, run_episode


def make_config(tmp_path, *, backend="in_process", workers=1, trials=3, recycle=50):
    config = {
        "modules": [{"name": "echo", "id": "echo"}],
        "tools": [],
        "monitors": [],
        "bodies": [],
        "environments": [{"name": "fake", "id": "env"}],
        "task_adapters": [{"name": "single-task", "params": {"num_trials": trials}}],
        "graphs": [
            {
                "id": "test",
                "entry": "reset",
                "outputs": {"success": {"source": "nodes.echo.outputs.success", "default": False}},
                "nodes": [
                    {'id': 'reset', 'type': 'system', 'operation': 'env.reset', 'next': [{'node': 'echo'}]},
                    {"id": "echo", "module": "echo", "predecessors": ["reset"]},
                ],
            }
        ],
        "parameters": {
            "task": {"task_id": "test", "env_id": "fake", "seed": 17},
            "runtime": {
                "environment_id": "env",
                "graph_id": "test",
                "monitor_enabled": False,
                "execution": {
                    "backend": backend,
                    "max_workers": workers,
                    "recycle_after_tasks": recycle,
                    "startup_timeout_s": 10,
                    "task_timeout_s": 10,
                    "shutdown_timeout_s": 1,
                    "memory_mode": "disabled",
                },
            },
        },
        "observation_cache": {"enabled": False},
        "safety_state": {"backend": "memory"},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


@pytest.mark.parametrize(
    "backend,workers", [("in_process", 1), ("process", 1), ("process", 2)]
)
def test_experiment_executes_same_graph_with_isolated_attempts(
    tmp_path, backend, workers
):
    config = make_config(tmp_path, backend=backend, workers=workers, recycle=2)
    result = asyncio.run(
        run_experiment(
            ExperimentRequest(
                config=config, output_dir=tmp_path / "runs", run_id="test-run"
            )
        )
    )
    assert result["num_errors"] == 0, result
    status = json.loads((tmp_path / "runs/test-run/status.json").read_text())
    assert status["completed_episodes"] == 3
    records = [
        json.loads(p.read_text())
        for p in (tmp_path / "runs/test-run/test").glob("episode_*.json")
    ]
    assert len(records) == 3
    live_records = [
        json.loads(p.read_text())
        for p in (tmp_path / "runs/test-run/test/.live").glob("episode_*.json")
    ]
    assert len(live_records) == 3
    assert all(item["status"] == "completed" for item in live_records)
    assert all(r["result"]["graph_success"] for r in records), records
    assert len({r["result"]["identity"]["episode_id"] for r in records}) == 3
    if backend == "process":
        source = json.loads((tmp_path / "runs/test-run/observability.json").read_text())
        reader = TraceQuery(source["trace_db"])
        for record in records:
            identity = record["result"]["identity"]
            assert reader.episode(
                run_id=identity["run_id"],
                episode_id=identity["episode_id"],
                attempt_id=identity["attempt_id"],
            )
        assert not Path(source["trace_socket"]).exists()


@pytest.mark.parametrize("backend", ["in_process", "process"])
def test_single_task_uses_same_worker_lifecycle(tmp_path, backend):
    config = make_config(tmp_path, backend=backend)
    result = asyncio.run(
        run_episode(EpisodeRequest(config=config, trace_dir=tmp_path / "trace", output=tmp_path / "episode.json"))
    )
    assert result["graph_success"] is True
    identity = result["identity"]
    rows = TraceQuery(result["trace_sqlite"]).episode(
        run_id=identity["run_id"], episode_id=identity["episode_id"]
    )
    assert rows


def test_optional_summary_failure_preserves_episode_result(tmp_path, monkeypatch):
    from robotenon.runtime import runner

    async def fail_summary(*args):
        raise RuntimeError("summary model unavailable")

    monkeypatch.setattr(runner, "_summarize_episode", fail_summary)
    config = make_config(tmp_path)
    value = json.loads(config.read_text())
    value["parameters"]["runtime"]["execution"]["memory_mode"] = "snapshot"
    config.write_text(json.dumps(value))
    result = asyncio.run(run_episode(EpisodeRequest(
        config=config, output=tmp_path / "episode.json", memory=tmp_path / "memory.json",
    )))
    assert result["graph_success"] is True
    assert result["ltm_summary"]["error"] == "RuntimeError: summary model unavailable"


def test_failed_reset_keeps_primary_error_without_summary(tmp_path, monkeypatch):
    from robotenon.runtime import runner
    from robotenon.envs.base.fake import FakeEnvironmentSession

    async def fail_reset(*args, **kwargs):
        raise RuntimeError("reset has no observations")

    async def unexpected_summary(*args):
        raise AssertionError("failed reset must not invoke summary model")

    monkeypatch.setattr(FakeEnvironmentSession, "reset", fail_reset)
    monkeypatch.setattr(runner, "_summarize_episode", unexpected_summary)
    config = make_config(tmp_path)
    value = json.loads(config.read_text())
    value["parameters"]["runtime"]["execution"]["memory_mode"] = "snapshot"
    config.write_text(json.dumps(value))
    result = asyncio.run(run_episode(EpisodeRequest(
        config=config, output=tmp_path / "episode.json", memory=tmp_path / "memory.json",
    )))
    assert result["graph_success"] is False
    assert "reset has no observations" in result["error"]
    assert result["ltm_summary"] is None
