import asyncio
import json
import multiprocessing
from queue import Empty

import numpy as np
import pytest
from batch_fixtures import EchoProvider

from robotenon.runtime.batch_services import SharedBatchServices
from robotenon.runtime.batch_transport import BatchClient, decode, encode
from robotenon.runtime.batching import BatchEngine
from robotenon.runtime.scheduling import run_bounded


def _hold_startup_slot(root, ready, release):
    from robotenon.runtime.startup_gate import (
        acquire_startup_gate,
        mark_worker_initialized,
    )

    acquire_startup_gate(2, root)
    ready.put(True)
    release.wait(5)
    mark_worker_initialized()


def test_native_startup_gate_spans_processes_until_environment_ready(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    releases = [context.Event() for _ in range(3)]
    processes = [
        context.Process(
            target=_hold_startup_slot,
            args=(str(tmp_path / "startup"), ready, releases[index]),
        )
        for index in range(3)
    ]
    try:
        for process in processes:
            process.start()
        assert ready.get(timeout=5)
        assert ready.get(timeout=5)
        with pytest.raises(Empty):
            ready.get(timeout=0.2)
        releases[0].set()
        assert ready.get(timeout=5)
    finally:
        for release in releases:
            release.set()
        for process in processes:
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
        ready.close()


def test_dynamic_batch_reorders_responses_and_tail_and_session_isolation():
    async def run():
        provider = EchoProvider()
        engine = BatchEngine(provider, max_batch_size=3, max_wait_ms=10)
        try:
            for key in ["a", "b", "c"]:
                await engine.open_session(key, {})
            results = await asyncio.gather(
                *(engine.infer(k, k) for k in ["a", "b", "c"])
            )
            assert [r["value"] for r in results] == ["a", "b", "c"]
            assert [r["batch_size"] for r in results] == [3, 3, 3]
            await engine.close_session("b")
            await engine.open_session("replacement", {})
            results = await asyncio.gather(
                engine.infer("a", 1), engine.infer("replacement", 2)
            )
            assert [r["count"] for r in results] == [2, 1]
            assert all(r["batch_size"] == 2 for r in results)
        finally:
            await engine.close()

    asyncio.run(run())


def test_incompatible_groups_and_bad_response_fail_only_batch():
    class Provider(EchoProvider):
        def batch_key(self, request):
            return request["observation"]["group"]

        def infer_batch(self, requests):
            assert len({r["observation"]["group"] for r in requests}) == 1
            if requests[0]["observation"]["group"] == "bad":
                return []
            return super().infer_batch(requests)

    async def run():
        engine = BatchEngine(Provider(), max_batch_size=4, max_wait_ms=2)
        try:
            for key in ["a", "b", "c"]:
                await engine.open_session(key, {})
            results = await asyncio.gather(
                engine.infer("a", {"group": "bad"}),
                engine.infer("b", {"group": "good"}),
                engine.infer("c", {"group": "good"}),
                return_exceptions=True,
            )
            assert isinstance(results[0], ValueError)
            assert [r["batch_size"] for r in results[1:]] == [2, 2]
        finally:
            await engine.close()

    asyncio.run(run())


def test_cancellation_pending_and_late_response_never_reaches_replacement():
    async def run():
        engine = BatchEngine(EchoProvider(delay=0.05), max_batch_size=1)
        try:
            await engine.open_session("old", {})
            task = asyncio.create_task(engine.infer("old", "old"))
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await engine.close_session("old")
            await engine.open_session("new", {})
            assert (await engine.infer("new", "new"))["value"] == "new"
        finally:
            await engine.close()

    asyncio.run(run())


def test_refill_does_not_wait_for_slow_job_and_bounds_concurrency():
    async def run():
        events = []
        active = 0

        async def execute(i):
            nonlocal active
            active += 1
            assert active <= 2
            events.append(("start", i))
            await asyncio.sleep(0.08 if i == 0 else 0.005)
            events.append(("end", i))
            active -= 1
            return i

        assert await run_bounded(range(5), execute, max_workers=2) == list(range(5))
        assert events.index(("start", 2)) < events.index(("end", 0))

    asyncio.run(run())


def test_wire_preserves_array_dtype_shape_and_does_not_confuse_user_dict():
    value = {
        "t": "array",
        "v": np.arange(18, dtype=np.uint8).reshape(2, 3, 3),
        "state": np.array([1, 2], dtype=np.float32),
    }
    result = decode(encode(value))
    assert result["t"] == "array"
    np.testing.assert_array_equal(result["v"], value["v"])
    assert result["state"].dtype == np.float32
    with pytest.raises(TypeError):
        encode(np.array([object()]))


@pytest.mark.parametrize("mode,expected_size", [("single", 1), ("batch", 3)])
def test_real_process_service_config_mode_and_sessions(tmp_path, mode, expected_size):
    async def run():
        services = SharedBatchServices(
            [
                {
                    "name": "model",
                    "factory": "batch_fixtures:EchoProvider",
                    "mode": mode,
                    "max_batch_size": 3,
                    "max_wait_ms": 100,
                }
            ],
            tmp_path,
            startup_timeout_s=10,
            shutdown_timeout_s=2,
        )
        clients = []
        try:
            await services.start()
            live_metrics = json.loads((tmp_path / "0-0.json").read_text())
            assert live_metrics["status"] == "running"
            assert live_metrics["requests"] == 0
            endpoint = services.for_worker(0)["model"]
            clients = [BatchClient(endpoint) for _ in range(3)]
            await asyncio.gather(*(c.open({}) for c in clients))
            results = await asyncio.gather(*(c.infer(i) for i, c in enumerate(clients)))
            assert [r["value"] for r in results] == [0, 1, 2]
            assert all(r["batch_size"] == expected_size for r in results)
            if mode == "batch":
                for _ in range(70):
                    live_metrics = json.loads((tmp_path / "0-0.json").read_text())
                    if live_metrics["requests"] == 3:
                        break
                    await asyncio.sleep(.1)
                assert live_metrics["requests"] == 3
                assert live_metrics["status"] == "running"
        finally:
            await asyncio.gather(*(c.close() for c in clients))
            await services.close()
        metrics = json.loads((tmp_path / "0-0.json").read_text())
        assert metrics["requests"] == 3
        assert metrics["status"] == "closed"
        assert metrics["pending_peak"] >= 1
        assert metrics["batch_sizes"] == {str(expected_size): 3 // expected_size}

    asyncio.run(run())


def test_replica_device_binding_and_worker_mapping(tmp_path):
    async def run():
        services = SharedBatchServices(
            [
                {
                    "name": "model",
                    "factory": "batch_fixtures:EchoProvider",
                    "devices": ["0", "1"],
                }
            ],
            tmp_path,
            startup_timeout_s=10,
            shutdown_timeout_s=2,
        )
        clients = []
        try:
            await services.start()
            for i in range(4):
                client = BatchClient(services.for_worker(i, str(i // 2))["model"])
                await client.open({})
                clients.append(client)
            results = await asyncio.gather(*(c.infer(i) for i, c in enumerate(clients)))
            assert [r["device"] for r in results] == ["0", "0", "1", "1"]
            assert results[0]["pid"] == results[1]["pid"]
            assert results[2]["pid"] == results[3]["pid"]
            assert results[0]["pid"] != results[2]["pid"]
        finally:
            await asyncio.gather(*(c.close() for c in clients))
            await services.close()

    asyncio.run(run())


def test_model_replicas_start_concurrently(tmp_path):
    async def run():
        services = SharedBatchServices(
            [
                {
                    "name": "model",
                    "factory": "batch_fixtures:EchoProvider",
                    "params": {"startup_delay": 0.4},
                    "devices": ["0", "1", "2"],
                }
            ],
            tmp_path,
            startup_timeout_s=10,
            shutdown_timeout_s=2,
        )
        clients = []
        try:
            await services.start()
            for index in range(3):
                client = BatchClient(services.for_worker(index)["model"])
                await client.open({})
                clients.append(client)
            results = await asyncio.gather(
                *(client.infer(index) for index, client in enumerate(clients))
            )
            created = [result["created_at"] for result in results]
            assert max(created) - min(created) < 0.6
        finally:
            await asyncio.gather(*(client.close() for client in clients))
            await services.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_generic_graph_process_workers_share_model_and_refill(tmp_path, mode):
    from test_execution_backends import make_config

    from robotenon.runtime.experiment import ExperimentRequest, run_experiment

    path = make_config(tmp_path, backend="process", workers=2, trials=5)
    config = json.loads(path.read_text())
    config["plugin_modules"] = ["batch_fixtures"]
    config["modules"] = [{"name": "batch_test_policy", "id": "echo", "kind": "agent"}]
    config["plugin_tools"] = ["robotenon.tools.batch_model"]
    config["tools"] = [
        {
            "name": "shared_batch_model",
            "id": "model",
            "kind": "model",
            "params": {"service": "vla"},
        }
    ]
    config["parameters"]["runtime"]["execution"]["batch_services"] = [
        {
            "name": "vla",
            "factory": "batch_fixtures:EchoProvider",
            "mode": mode,
            "max_batch_size": 2,
            "max_wait_ms": 100,
        }
    ]
    path.write_text(json.dumps(config))
    result = asyncio.run(
        run_experiment(
            ExperimentRequest(config=path, output_dir=tmp_path / "runs", run_id="batch")
        )
    )
    assert result["num_errors"] == 0, result
    assert result["num_episodes"] == 5
    assert result["successes"] == 5
    metrics = json.loads((tmp_path / "runs/batch/batch-metrics/0-0.json").read_text())
    assert metrics["requests"] == 15
    if mode == "single":
        assert metrics["batch_sizes"] == {"1": 15}
    else:
        assert metrics["batch_sizes"].get("2", 0) > 0


def test_disconnect_cancels_queued_request_and_timeout_does_not_poison_service(
    tmp_path,
):
    async def run():
        services = SharedBatchServices(
            [
                {
                    "name": "model",
                    "factory": "batch_fixtures:EchoProvider",
                    "max_wait_ms": 200,
                    "max_batch_size": 2,
                }
            ],
            tmp_path,
            startup_timeout_s=10,
            shutdown_timeout_s=2,
        )
        old = new = None
        try:
            await services.start()
            endpoint = services.for_worker(0)["model"]
            old = BatchClient(endpoint, timeout_s=0.04)
            await old.open({})
            with pytest.raises(asyncio.TimeoutError):
                await old.infer("old")
            new = BatchClient(endpoint)
            await new.open({})
            result = await new.infer("new")
            assert result["value"] == "new"
            assert result["count"] == 1
            assert result["batch_size"] == 1
        finally:
            for client in [old, new]:
                if client:
                    await client.close()
            await services.close()
        assert json.loads((tmp_path / "0-0.json").read_text())["requests"] == 1

    asyncio.run(run())


def test_model_process_death_detected_and_connections_fail(tmp_path):
    async def run():
        services = SharedBatchServices(
            [{"name": "model", "factory": "batch_fixtures:EchoProvider"}],
            tmp_path,
            startup_timeout_s=10,
            shutdown_timeout_s=1,
        )
        client = None
        try:
            await services.start()
            client = BatchClient(services.for_worker(0)["model"], timeout_s=1)
            await client.open({})
            process = services.processes[0][0]
            process.terminate()
            await asyncio.to_thread(process.join, 2)
            with pytest.raises(RuntimeError, match="exited"):
                services.check_health()
            with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
                await client.infer("lost")
        finally:
            if client:
                await client.disconnect()
            await services.close()

    asyncio.run(run())


def test_overlapping_session_rejected_and_queue_backpressure_releases_on_close():
    async def run():
        engine = BatchEngine(
            EchoProvider(), max_batch_size=1, max_pending=1, max_wait_ms=100
        )
        # Hold provider serialization to keep first request in flight.
        await engine.open_session("a", {})
        await engine.open_session("b", {})
        await engine.lock.acquire()
        a = asyncio.create_task(engine.infer("a", 1))
        await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError, match="busy"):
            await engine.infer("a", 2)
        b = asyncio.create_task(engine.infer("b", 3))
        await asyncio.sleep(0.01)
        assert not b.done()
        engine.lock.release()
        assert (await a)["value"] == 1
        assert (await b)["value"] == 3
        await engine.close()

    asyncio.run(run())


def test_execution_devices_validation_and_resource_reservation():
    from robotenon.infra.params import Params
    from robotenon.runtime.execution import ExecutionOptions

    cfg = {
        "runtime": {
            "execution": {
                "backend": "process",
                "max_workers": 4,
                "worker_devices": ["0", "0", "1", "1"],
                "batch_services": [
                    {
                        "name": "vla",
                        "factory": "batch_fixtures:EchoProvider",
                        "devices": ["2", "3"],
                    }
                ],
                "external_batch_services": {
                    "remote": ["/tmp/remote-0.sock"] * 2 + ["/tmp/remote-1.sock"] * 2
                },
                "worker_startup_parallelism": 2,
                "native_reset_parallelism": 2,
                "native_reset_gate": "/tmp/robotenon-test-native-reset",
            }
        }
    }
    options = ExecutionOptions.from_params(Params(cfg))
    assert options.worker_gpus(1) == ("0",)
    assert options.resource_gpu_ids == ("0", "1", "2", "3")
    assert options.external_batch_services["remote"][2] == "/tmp/remote-1.sock"
    assert options.worker_startup_parallelism == 2
    assert options.native_reset_parallelism == 2
    cfg["runtime"]["execution"]["worker_devices"] = ["0"]
    with pytest.raises(ValueError, match="one device per worker"):
        ExecutionOptions.from_params(Params(cfg))

    cfg["runtime"]["execution"]["worker_devices"] = ["0", "0", "1", "1"]
    cfg["runtime"]["execution"]["external_batch_services"]["remote"] = [
        "/tmp/a.sock",
        "/tmp/b.sock",
    ]
    with pytest.raises(ValueError, match="one endpoint or one per Worker"):
        ExecutionOptions.from_params(Params(cfg))


def test_async_provider_uses_persistent_model_loop_and_thread():
    import threading

    class AsyncProvider:
        async def open_session(self, session_id, metadata):
            self.loop = asyncio.get_running_loop()
            self.thread = threading.get_ident()

        async def infer_batch(self, requests):
            assert asyncio.get_running_loop() is self.loop
            assert threading.get_ident() == self.thread
            return [{"request_id": r["request_id"], "output": True} for r in requests]

        async def close_session(self, session_id, result):
            assert asyncio.get_running_loop() is self.loop

    async def run():
        engine = BatchEngine(AsyncProvider())
        await engine.open_session("a", {})
        assert await engine.infer("a", {}) is True
        await engine.close()

    asyncio.run(run())


def test_partial_service_startup_failure_cleans_processes(tmp_path):
    async def run():
        services = SharedBatchServices(
            [
                {"name": "good", "factory": "batch_fixtures:EchoProvider"},
                {"name": "bad", "factory": "batch_fixtures:MissingProvider"},
            ],
            tmp_path,
            startup_timeout_s=3,
            shutdown_timeout_s=0.2,
        )
        with pytest.raises(RuntimeError, match="MissingProvider"):
            await services.start()
        assert not services.processes
        assert services.directory is None
        await services.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "unknown"},
        {"max_batch_size": 0},
        {"max_batch_size": True},
        {"max_wait_ms": float("nan")},
        {"max_pending": 1, "max_batch_size": 2},
        {"devices": ["0", "0"]},
    ],
)
def test_invalid_batch_config_rejected(options):
    from robotenon.runtime.batch_services import BatchServiceConfig

    with pytest.raises(ValueError):
        BatchServiceConfig.parse(
            {"name": "vla", "factory": "batch_fixtures:EchoProvider", **options}
        )


def test_example_config_runs_without_benchmark_extension(tmp_path):
    from robotenon.runtime.experiment import ExperimentRequest, run_experiment

    result = asyncio.run(
        run_experiment(
            ExperimentRequest(
                config="examples/graph_batching/config.json",
                output_dir=tmp_path,
                run_id="example",
            )
        )
    )
    assert result["num_errors"] == 0, result
    assert result["num_episodes"] == result["successes"] == 7


def test_standalone_service_supports_external_graph_clients(tmp_path):
    import os
    import signal
    import sys
    from pathlib import Path

    async def run():
        config = tmp_path / "services.json"
        ready = tmp_path / "ready.json"
        config.write_text(
            json.dumps(
                [
                    {
                        "name": "echo",
                        "factory": "examples.graph_batching.providers:EchoModel",
                        "mode": "single",
                    }
                ]
            )
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "robotenon.runtime.batch_services",
            "--config",
            str(config),
            "--ready-file",
            str(ready),
            "--metrics-dir",
            str(tmp_path / "metrics"),
            env=dict(os.environ, PYTHONPATH=str(Path.cwd())),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        client = None
        try:
            for _ in range(200):
                if ready.exists():
                    break
                if process.returncode is not None:
                    raise AssertionError((await process.stderr.read()).decode())
                await asyncio.sleep(0.025)
            routes = json.loads(ready.read_text())
            client = BatchClient(routes["echo"][0][1])
            await client.open({})
            assert (await client.infer({"question": "hello"}))["answer"] == "hello"
            await client.close()
        finally:
            if client:
                await client.disconnect()
            if process.returncode is None:
                process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0
        assert not ready.exists()
        assert json.loads((tmp_path / "metrics/0-0.json").read_text())["requests"] == 1

    asyncio.run(run())
