"""Native Graph integration, real worker and model processes; fixture physics."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))

from parity_fixtures import StatefulBenchmark
from robotenon_benchmark_support.configure import (
    application_config,
    enable_batch_execution,
    write_application_config,
)
from robotenon_benchmark_support.integration import task_manifest

from robotenon.runtime.experiment import ExperimentRequest, run_experiment


@pytest.mark.parametrize(
    "interface,factory",
    [("batch", "batch_fixtures:BatchVLA"), ("legacy", "parity_fixtures:create")],
)
@pytest.mark.parametrize("mode", ["single", "batch"])
def test_native_graph_parallel_model_and_refill(tmp_path, interface, factory, mode):
    config = application_config(
        {
            "benchmark_id": "parity_benchmark",
            "params": {},
            "episodes_per_task": 3,
            "max_steps": 3,
        },
        policy_factory=factory,
    )
    for key in ("plugin_environments", "plugin_bodies", "plugin_task_adapters"):
        config[key] = ["parity_fixtures"]
    config["task_adapters"][0]["params"]["manifest"] = task_manifest(
        StatefulBenchmark()
    )
    config["safety_state"] = {"backend": "memory", "require_shared": False}
    enable_batch_execution(
        config,
        workers=2,
        max_batch_size=2,
        max_wait_ms=100,
        interface=interface,
        inference_mode=mode,
    )
    config["parameters"]["runtime"]["execution"]["startup_timeout_s"] = 20
    config["parameters"]["runtime"]["execution"]["shutdown_timeout_s"] = 2
    path = tmp_path / "config.json"
    write_application_config(config, path)
    result = asyncio.run(
        run_experiment(
            ExperimentRequest(
                config=path, output_dir=tmp_path / "runs", run_id="native"
            )
        )
    )
    assert result["num_errors"] == 0, result
    assert result["num_episodes"] == 6
    assert result["successes"] == 6
    metrics = json.loads((tmp_path / "runs/native/batch-metrics/0-0.json").read_text())
    assert metrics["requests"] == 18
    if mode == "single":
        assert metrics["batch_sizes"] == {"1": 18}
    else:
        assert metrics["batch_sizes"].get("2", 0) > 0


CATALOG = json.loads((ROOT / "docs/benchmark-catalog.json").read_text())


@pytest.mark.parametrize("entry", CATALOG, ids=lambda e: e["id"])
def test_each_extension_exposes_batch_vla_and_generated_config(entry, tmp_path):
    import importlib

    sys.path.insert(0, str(ROOT / entry["path"] / "src"))
    module = importlib.import_module(f"robotenon_{entry['id']}.vla")
    model = module.create_batch()
    model.open_session("a", {})
    model.open_session("b", {})
    requests = [
        {
            "request_id": key,
            "session_id": key,
            "step_id": 0,
            "observation": {"task_description": "test"},
        }
        for key in ["a", "b"]
    ]
    results = model.act_batch(requests)
    assert [r["request_id"] for r in results] == ["a", "b"]
    model.close_session("a", {"status": "completed"})
    assert len(model.act_batch(requests[1:])) == 1
    model.close()
    cfg = application_config(
        {"benchmark_id": entry["registry"], "params": {}},
        policy_factory=f"robotenon_{entry['id']}.vla:create_batch",
    )
    graph = json.dumps(cfg["graphs"], sort_keys=True)
    enable_batch_execution(
        cfg, workers=4, worker_devices=["0", "0", "1", "1"], model_devices=["0", "1"]
    )
    assert json.dumps(cfg["graphs"], sort_keys=True) == graph
    from robotenon.runtime.config import load_application_config

    path = tmp_path / "batch.json"
    write_application_config(cfg, path)
    load_application_config(path)


def test_cli_generates_batch_factory_and_single_mode(tmp_path, monkeypatch):
    from robotenon_benchmark_support.configure import main

    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"benchmark_id": "libero_benchmark", "params": {}}))
    path = tmp_path / "batch.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "configure",
            "--profile",
            str(profile),
            "--output",
            str(path),
            "--batch-interface",
            "batch",
            "--workers",
            "2",
            "--inference-mode",
            "single",
        ],
    )
    main()
    cfg = json.loads(path.read_text())
    service = cfg["parameters"]["runtime"]["execution"]["batch_services"][0]
    assert service["mode"] == "single"
    assert service["params"]["factory"] == "robotenon_libero.vla:create_batch"
    assert (tmp_path / "batch_graphs.json").exists()
    assert (tmp_path / "batch_modules.json").exists()


def test_xiaomi_worker_keeps_history_and_chunk_locally_and_isolates_sessions():
    from types import SimpleNamespace

    import numpy as np
    from robotenon_benchmark_support.codec import pack_observation
    from robotenon_benchmark_support.runtime import BackendRuntime as XiaomiNativeBatch

    class Tool(XiaomiNativeBatch):
        def __init__(self):
            super().__init__(service="test", declaration={"history": {"length": 4, "interval": 2}, "replan_steps": 16})
            self.requests = []

        async def infer(self, session_id, payload, metadata):
            self.requests.append((session_id, payload))
            action = 1 if session_id == "a" else 2
            return {"native_actions": [{"actions": [action]}, {"actions": [action]}]}

    async def run():
        tool = Tool()
        for step in range(4):
            for session in ["a", "b"]:
                observation = {
                    "task_description": "test",
                    "images": {"front": np.full((2, 2, 3), step, dtype=np.uint8)},
                }
                context = SimpleNamespace(
                    session_id=session,
                    observation=None,
                    feedback=SimpleNamespace(
                        observation=pack_observation(observation),
                        metadata={"native_task": {"name": session}, "benchmark": "test", "native_case": session},
                    ),
                )
                result = await tool.call({}, context)
                assert result.output["native_action"]["actions"] == [
                    1 if session == "a" else 2
                ]
        assert len(tool.requests) == 4  # 8 native steps, only 4 inference requests.
        first = tool.requests[0][1]["history"]
        second = tool.requests[2][1]["history"]
        assert [int(o["images"]["front"][0, 0, 0]) for o in first] == [0, 0, 0, 0]
        assert [int(o["images"]["front"][0, 0, 0]) for o in second] == [0, 0, 0, 2]
        await tool.end_session("a")
        assert "a" not in tool.histories and "b" in tool.histories
        await tool.close()
        assert not tool.histories and not tool.actions

    asyncio.run(run())
