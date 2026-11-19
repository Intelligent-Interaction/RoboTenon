"""Expected-behaviour regressions for the independent 2026-09-20 audit."""

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_execution_backends import make_config

from robotenon.infra.params import Params
from robotenon.modules.verify import VerifyModule
from robotenon.runtime.experiment import ExperimentRequest, run_experiment
from robotenon.runtime.runner import EpisodeRequest
from robotenon.runtime.worker import WorkerRuntime


def test_fd_monitor_records_process_types_and_episode_delta(tmp_path):
    from robotenon.runtime.fd_monitor import FileDescriptorMonitor

    monitor = FileDescriptorMonitor(tmp_path)
    first = monitor.snapshot("experiment_start", timestamp="2026-09-21T00:00:00Z")
    read_fd, write_fd = os.pipe()
    held_file = (tmp_path / "held.log").open("w")
    try:
        second = monitor.snapshot(
            "episode_complete",
            episode={"task_id": "task", "global_index": 3},
            timestamp="2026-09-21T00:01:00Z",
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
        held_file.close()

    process = next(row for row in second["processes"] if row["pid"] == os.getpid())
    assert process["role"] == "coordinator+trace+metrics"
    assert process["delta"] >= 3
    assert process["types"]["file"] >= 1
    assert second["episode"] == {"task_id": "task", "global_index": 3}
    assert len((tmp_path / "fd-usage.jsonl").read_text().splitlines()) == 2
    assert first["event"] == "experiment_start"


def test_ingest_server_rejects_excess_connections_before_fd_growth(tmp_path):
    from robotenon.infra.ingest_server import IngestServer

    async def scenario():
        import tempfile

        entered = threading.Event()
        release = threading.Event()

        def ingest(_batch):
            entered.set()
            assert release.wait(3)

        socket_dir = tempfile.TemporaryDirectory(prefix="ingest-test-", dir="/tmp")
        server = await IngestServer(
            Path(socket_dir.name) / "bounded.sock",
            SimpleNamespace(ingest_batch=ingest),
            max_connections=1,
        ).start()
        first_reader, first_writer = await asyncio.open_unix_connection(
            str(server.path)
        )
        first_writer.write(b"{}\n")
        await first_writer.drain()
        assert await asyncio.to_thread(entered.wait, 1)
        second_reader, second_writer = await asyncio.open_unix_connection(
            str(server.path)
        )
        try:
            response = json.loads(await asyncio.wait_for(second_reader.readline(), 1))
            assert response == {"ok": False, "error": "observability ingest busy"}
            assert len(server.handlers) == 1
        finally:
            second_writer.close()
            await second_writer.wait_closed()
            release.set()
            assert json.loads(await asyncio.wait_for(first_reader.readline(), 1)) == {
                "ok": True
            }
            first_writer.close()
            await first_writer.wait_closed()
            await server.close()
            socket_dir.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "output",
    [
        None,
        {},
        {"subtask_success": "false"},
        {"subtask_success": "true"},
        {"subtask_success": 0},
        {"subtask_success": 1},
        {"subtask_success": None},
        {"subtask_success": []},
        {"subtask_success": {}},
        {"subtask_success": True, "physical_belief_update": []},
    ],
)
def test_verifier_rejects_invalid_output_without_mutating_memory(output):
    async def scenario():
        async def tool(*args):
            return SimpleNamespace(ok=True, output=output, metadata={})

        ctx = SimpleNamespace(
            inputs={"instruction": "pick"},
            values={"unchanged": True},
            feedback=None,
            params=Params({"model": {"hlp_tool": "fake"}, "task": {"task_id": "pick"}}),
            call_tool=tool,
        )
        with pytest.raises(ValueError, match="verifier"):
            await VerifyModule().run(ctx)
        assert ctx.values == {"unchanged": True}

    asyncio.run(scenario())


@pytest.mark.parametrize("success", [False, True])
def test_verifier_accepts_only_actual_boolean(success):
    async def scenario():
        async def tool(*args):
            return SimpleNamespace(
                ok=True, output={"subtask_success": success}, metadata={}
            )

        ctx = SimpleNamespace(
            inputs={"instruction": "pick"},
            values={},
            feedback=None,
            params=Params({"model": {"hlp_tool": "fake"}, "task": {"task_id": "pick"}}),
            call_tool=tool,
        )
        result = await VerifyModule().run(ctx)
        assert result["subtask_success"] is success
        assert result["stm"]["completed"] == (["pick"] if success else [])
        assert result["stm"]["remaining"] == ([] if success else ["pick"])

    asyncio.run(scenario())


def test_worker_uses_snapshot_after_config_file_changes(tmp_path):
    async def scenario():
        config = make_config(tmp_path, trials=1)
        value = json.loads(config.read_text())
        value["modules"][0]["params"] = {"prefix": "before"}
        value["graphs"][0]["outputs"]["marker"] = "nodes.echo.outputs.prefix"
        config.write_text(json.dumps(value))
        request = EpisodeRequest(config=config, output=tmp_path / "episode.json")
        worker = WorkerRuntime(request)
        try:
            first = await worker.run_task(request)
            value["modules"][0]["params"]["prefix"] = "after"
            config.write_text(json.dumps(value))
            second = await worker.run_task(request)
            assert first["outputs"]["marker"] == second["outputs"]["marker"] == "before"
        finally:
            await worker.close()
        fresh = WorkerRuntime(request)
        try:
            assert (await fresh.run_task(request))["outputs"]["marker"] == "after"
        finally:
            await fresh.close()

    asyncio.run(scenario())


def test_multiseed_cases_have_independent_running_slots(tmp_path):
    from robotenon.task_adapters.base import (
        BaseTaskAdapter,
        ConfiguredTaskAdapter,
        TaskCase,
    )
    from robotenon.task_adapters.evaluator import evaluate_task_adapters

    async def scenario():
        gate, fast_done = asyncio.Event(), asyncio.Event()
        calls, finished = [], []

        class Adapter(BaseTaskAdapter):
            def enumerate_cases(self, *, adapter_id, params):
                for index, seed in enumerate((10, 20)):
                    yield TaskCase(
                        adapter_id=adapter_id,
                        task_id="same",
                        environment_id="fake",
                        index=index,
                        seed=seed,
                    )

        class Backend:
            max_workers = 2

            async def execute(self, configured, case, context):
                calls.append(case.index)
                if case.index == 0:
                    await gate.wait()
                else:
                    fast_done.set()
                finished.append(case.index)
                return {"task_success": True}

        configured = ConfiguredTaskAdapter(
            "adapter", SimpleNamespace(name="fixture"), Adapter()
        )
        task = asyncio.create_task(
            evaluate_task_adapters(
                config_path=tmp_path / "config.json",
                params=Params({}),
                adapters=[configured],
                output_dir=tmp_path,
                run_id="seeds",
                run_episode=None,
                execution_backend=Backend(),
                options={"memory_mode": "disabled"},
            )
        )
        try:
            await asyncio.wait_for(fast_done.wait(), 1)
            await asyncio.sleep(0.15)
            assert not task.done()
            gate.set()
            result = await asyncio.wait_for(task, 1)
            assert result["num_completed"] == result["successes"] == 2
            assert sorted(calls) == sorted(finished) == [0, 1]
        finally:
            gate.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_duplicate_case_index_is_rejected_before_dispatch(tmp_path):
    from robotenon.task_adapters.base import (
        BaseTaskAdapter,
        ConfiguredTaskAdapter,
        TaskCase,
    )
    from robotenon.task_adapters.evaluator import evaluate_task_adapters

    class Adapter(BaseTaskAdapter):
        def enumerate_cases(self, *, adapter_id, params):
            for seed in (1, 2):
                yield TaskCase(
                    adapter_id=adapter_id,
                    task_id="same",
                    environment_id="fake",
                    index=0,
                    seed=seed,
                )

    configured = ConfiguredTaskAdapter(
        "adapter", SimpleNamespace(name="fixture"), Adapter()
    )
    with pytest.raises(ValueError, match="duplicate case identities"):
        asyncio.run(
            evaluate_task_adapters(
                config_path=tmp_path / "config.json",
                params=Params({}),
                adapters=[configured],
                output_dir=tmp_path,
                run_id="duplicate",
                run_episode=None,
            )
        )


def test_idle_ingest_connections_are_closed_without_client_cooperation():
    import tempfile
    from pathlib import Path
    from robotenon.infra.ingest_server import IngestServer

    async def scenario():
        with tempfile.TemporaryDirectory(
            dir="/tmp", prefix="robotenon-close-"
        ) as directory:
            server = await IngestServer(
                Path(directory) / "i.sock", SimpleNamespace(ingest_batch=lambda _: None)
            ).start()
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            writer.write(b"{")
            await writer.drain()
            await asyncio.sleep(0.01)
            try:
                await asyncio.wait_for(server.close(), 0.5)
                assert await asyncio.wait_for(reader.read(), 0.5) == b""
                assert not server.path.exists()
                assert not server.handlers
            finally:
                writer.close()
                await writer.wait_closed()
                await server.close()

    asyncio.run(scenario())


def test_worker_rebuilds_tools_after_session_cleanup_failure(tmp_path):
    from robotenon.tools.base import BaseTool
    from robotenon.tools.registry import register_tool

    events = []

    class PoisonTool(BaseTool):
        async def call(self, arguments, context):
            return {}

        async def start(self):
            events.append("start")

        async def end_session(self, session_id):
            events.append("cleanup_failed")
            raise RuntimeError("cleanup failed")

        async def close(self):
            events.append("close")

    name = "poison-" + uuid4().hex
    register_tool(name)(PoisonTool)
    config = make_config(tmp_path, trials=2)
    value = json.loads(config.read_text())
    value["tools"] = [{"name": name}]
    config.write_text(json.dumps(value))
    result = asyncio.run(
        run_experiment(
            ExperimentRequest(
                config=config, output_dir=tmp_path / "out", run_id="poison"
            )
        )
    )
    assert result["num_errors"] == 2
    assert events == ["start", "cleanup_failed", "close"] * 2


def test_external_body_cancel_stops_driver_and_blocks_reuse_until_it_exits():
    from robotenon.runtime.safety import SafetyStateStore
    from robotenon.services.body import InProcessBodyService
    from robotenon.schemas import Action, ActionChunk

    async def scenario():
        entered, release = threading.Event(), threading.Event()
        stops = []

        def execute(action):
            entered.set()
            release.wait(3)

        body = InProcessBodyService(
            execute,
            safety_state=SafetyStateStore(),
            stop_executor=stops.append,
            stop_timeout_s=0.03,
        )
        session = uuid4()
        chunk = lambda: ActionChunk(
            session_id=session, actions=[Action(session_id=session)]
        )
        task = asyncio.create_task(body.execute(chunk()))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stops == ["body_execution_cancelled"]
            assert (await body.execute(chunk())).status == "rejected"
            with pytest.raises(TimeoutError, match="driver is still running"):
                await body.close()
            assert (await body.health()).degraded
        finally:
            release.set()
            while body._sync_driver_busy.is_set():
                await asyncio.sleep(0.001)
            await body.close()

    asyncio.run(scenario())


def test_real_robot_sync_sdk_is_isolated_from_watchdog_and_deadline():
    import time
    from robotenon.bodies.real_robot_body import RealRobotBody
    from robotenon.runtime.safety import SafetyStateStore
    from robotenon.schemas import Action, ActionChunk

    async def scenario():
        release = threading.Event()
        calls, ticks = [], []
        loop_thread = threading.get_ident()

        class Driver:
            def connect(self):
                calls.append(("connect", threading.get_ident()))

            def execute(self, action):
                calls.append(("execute", threading.get_ident()))
                release.wait(3)

            def hold(self, reason):
                calls.append(("hold", threading.get_ident()))
                release.set()

            def close(self):
                calls.append(("close", threading.get_ident()))

        body = RealRobotBody(
            driver=Driver(), safety_state=SafetyStateStore(), stop_timeout_s=0.2
        )
        session = uuid4()
        asyncio.get_running_loop().call_later(
            0.01, lambda: ticks.append(time.monotonic())
        )
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                body.execute(
                    ActionChunk(
                        session_id=session,
                        actions=[Action(session_id=session, timeout_ms=25)],
                    )
                ),
                1,
            )
            assert result.deadline_exceeded and result.status == "failed"
            assert ticks and ticks[0] - started < 0.2
            assert [name for name, _ in calls].count("hold") == 1
        finally:
            release.set()
            await body.close()
        assert all(tid != loop_thread for _, tid in calls)
        assert (
            len({tid for name, tid in calls if name in {"connect", "execute", "close"}})
            == 1
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("durable", [False, True])
def test_idempotency_rejects_changed_payload_and_accepts_identical_retry(
    tmp_path, durable
):
    from robotenon.runtime.safety import SafetyStateStore
    from robotenon.services.body import InProcessBodyService
    from robotenon.schemas import Action, ActionChunk

    async def scenario():
        calls = []

        async def execute(action):
            calls.append(action.payload)

        def new_body():
            return InProcessBodyService(
                execute,
                safety_state=SafetyStateStore(),
                idempotency_db=tmp_path / "actions.sqlite3" if durable else None,
            )

        body = new_body()
        session = uuid4()

        async def run(payload, key="same"):
            return await body.execute(
                ActionChunk(
                    session_id=session,
                    actions=[
                        Action(session_id=session, idempotency_key=key, payload=payload)
                    ],
                )
            )

        try:
            first = await run({"target": [1, 2], "speed": 1})
            assert first.status == "succeeded"
            if durable:
                await body.close()
                body = new_body()
            retry = await run({"speed": 1, "target": [1, 2]})
            assert retry.status == "succeeded" and retry.executed_actions == 0
            conflict = await run({"speed": 1, "target": [3, 4]})
            assert (
                conflict.status == "rejected"
                and "idempotency_conflict" in conflict.error
            )
            assert len(calls) == 1
            assert (await run({"target": [3, 4]}, key="new")).status == "succeeded"
            assert len(calls) == 2
        finally:
            await body.close()

    asyncio.run(scenario())


def test_idempotency_legacy_success_cannot_acknowledge_unverified_request(tmp_path):
    from robotenon.runtime.safety import SafetyStateStore
    from robotenon.services.body import InProcessBodyService
    from robotenon.services.action_ledger import SQLiteActionExecutionStore
    from robotenon.schemas import Action, ActionChunk, ActionExecution

    async def scenario():
        session = uuid4()
        store = SQLiteActionExecutionStore(tmp_path / "old.sqlite3")
        store.save(
            session,
            ActionExecution(
                action_id=uuid4(), idempotency_key="old", status="succeeded"
            ),
        )
        calls = []
        body = InProcessBodyService(
            calls.append, safety_state=SafetyStateStore(), idempotency_store=store
        )
        try:
            result = await body.execute(
                ActionChunk(
                    session_id=session,
                    actions=[Action(session_id=session, idempotency_key="old")],
                )
            )
            assert result.status == "rejected" and calls == []
        finally:
            await body.close()
            store.close()

    asyncio.run(scenario())


def test_ingest_reports_unfinished_write_without_closing_underneath_it():
    import tempfile
    from pathlib import Path
    from robotenon.infra.ingest_server import IngestServer

    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def ingest(batch):
            entered.set()
            release.wait(3)

        with tempfile.TemporaryDirectory(
            dir="/tmp", prefix="robotenon-write-"
        ) as directory:
            server = await IngestServer(
                Path(directory) / "i.sock",
                SimpleNamespace(ingest_batch=ingest),
                close_timeout_s=0.03,
            ).start()
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            writer.write(b"{}\n")
            await writer.drain()
            try:
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                with pytest.raises(TimeoutError, match="write is still running"):
                    await asyncio.wait_for(server.close(), 0.5)
                assert any(not task.done() for task in server.writes)
            finally:
                release.set()
                writer.close()
                await writer.wait_closed()
                await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["in_process", "process"])
def test_frozen_config_crosses_execution_boundary(tmp_path, backend):
    from robotenon.runtime.config import load_application_config, freeze_config

    config = make_config(tmp_path, backend=backend, trials=1)
    value = json.loads(config.read_text())
    value["modules"][0]["params"] = {"prefix": "frozen"}
    value["graphs"][0]["outputs"]["marker"] = "nodes.echo.outputs.prefix"
    config.write_text(json.dumps(value))
    snapshot = freeze_config(load_application_config(config))
    value["modules"][0]["params"] = {"prefix": "mutated"}
    config.write_text(json.dumps(value))
    asyncio.run(
        run_experiment(
            ExperimentRequest(
                config=config,
                config_snapshot=snapshot,
                output_dir=tmp_path / "runs",
                run_id="frozen",
            )
        )
    )
    episode = next((tmp_path / "runs/frozen/test").glob("episode_*.json"))
    assert json.loads(episode.read_text())["result"]["outputs"]["marker"] == "frozen"


def test_portal_queue_and_fork_keep_complete_config_snapshot(tmp_path, monkeypatch):
    from robotenon.web.experiments import ExperimentManager

    config = make_config(tmp_path, trials=1)
    manager = ExperimentManager(
        project_root=tmp_path, config_root=".", output_root="runs"
    )
    monkeypatch.setattr(manager, "_dispatch_waiting", lambda: None)
    job = manager.submit(config)
    original = manager._load_job_config(job).params.content_hash
    value = json.loads(config.read_text())
    value["modules"][0]["params"] = {"prefix": "changed"}
    config.write_text(json.dumps(value))
    assert manager._load_job_config(job).params.content_hash == original
    child = manager.fork(job["job_id"])
    assert manager._load_job_config(child).params.content_hash == original
    assert "--config-snapshot" in child["command"]


def test_new_run_rejects_overwrite_but_explicit_resume_works(tmp_path):
    config = make_config(tmp_path, trials=1)
    request = ExperimentRequest(
        config=config, output_dir=tmp_path / "runs", run_id="same"
    )
    asyncio.run(run_experiment(request))
    root = tmp_path / "runs/same"
    episode = next((root / "test").glob("episode_*.json"))
    old = episode.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(run_experiment(request))
    assert episode.read_bytes() == old
    (root / ".control/resume.json").write_text(
        json.dumps({"action": "rerun", "tasks": None})
    )
    asyncio.run(run_experiment(request))
    assert episode.read_bytes() != old


def test_run_reservation_is_atomic_and_allows_precreated_portal_directory(tmp_path):
    from robotenon.runtime.run_state import RunReservation

    root = tmp_path / "precreated"
    root.mkdir()
    (root / "experiment_override.json").write_text("{}")
    first = RunReservation(tmp_path, "precreated", "hash").acquire()
    try:
        with pytest.raises(RuntimeError, match="already owned"):
            RunReservation(tmp_path, "precreated", "hash").acquire()
    finally:
        first.close()
    with pytest.raises(ValueError, match="already exists"):
        RunReservation(tmp_path, "precreated", "hash").acquire()


def test_remote_portal_requires_and_checks_authentication():
    from email.message import Message
    from robotenon.web.access import PortalAccess

    with pytest.raises(ValueError, match="requires ROBOTENON_PORTAL_TOKEN"):
        PortalAccess("0.0.0.0", control=True)
    headers = Message()
    headers["Host"] = "127.0.0.1:8765"
    headers["Content-Type"] = "application/json"
    access = PortalAccess("0.0.0.0", control=True, token="configured-secret")
    assert access.check(headers, write=True, control=True)[0] == 401
    headers["Authorization"] = "Bearer configured-secret"
    assert access.check(headers, write=True, control=True) is None
    headers.replace_header("Authorization", "Bearer wrong")
    assert access.check(headers, write=True, control=True)[0] == 401
    headers.replace_header("Authorization", "Basic dXNlcjpjb25maWd1cmVkLXNlY3JldA==")
    assert access.check(headers, write=True, control=True) is None


def test_compose_exposes_only_loopback_and_anonymous_viewing():
    from pathlib import Path
    import re

    text = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
    ports = re.findall(r"(?m)^    ports:\n((?:      - .*\n)+)", text)
    assert len(ports) == 4
    assert all(
        line.strip().startswith('- "127.0.0.1:')
        for group in ports
        for line in group.splitlines()
    )
    assert "GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer" in text
    assert "GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?" in text


@pytest.mark.parametrize("prekill", [False, True])
def test_portal_force_stop_reaps_independent_worker_and_model_and_keeps_lease(
    tmp_path, monkeypatch, prekill
):
    import os
    import signal
    import sys
    import time
    from pathlib import Path
    from robotenon.runtime.execution import ResourceLease
    from robotenon.runtime.processes import process_table
    from robotenon.web.experiments import ExperimentManager

    module_name = "stop_fixture_" + uuid4().hex
    worker_pid_file = tmp_path / "worker.pid"
    model_pid_file = tmp_path / "model.pid"
    (tmp_path / (module_name + ".py")).write_text("""
import os, time
from pathlib import Path
from robotenon.tools.base import BaseTool
from robotenon.tools.registry import register_tool
from robotenon.task_adapters.single import SingleTaskAdapter
from robotenon.task_adapters import register_task_adapter
@register_task_adapter('blocking-worker', replace=True)
class Blocking(SingleTaskAdapter):
    async def run_case(self, case, context):
        Path(context.params.resolve('worker_pid_file')).write_text(str(os.getpid()))
        time.sleep(30)
        return {'task_success': False}
@register_tool('stop-test-model', replace=True)
class Client(BaseTool):
    def bind_deployment(self, host, port, model_path): pass
    async def call(self, arguments, context): return {}
""")
    server_script = tmp_path / "model.py"
    server_script.write_text("""
import os, signal, socket, sys
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[3]).write_text(str(os.getpid()))
with socket.socket() as server:
    server.bind((sys.argv[1], int(sys.argv[2])))
    server.listen()
    while True:
        connection, _ = server.accept()
        connection.close()
""")
    entrypoint = tmp_path / "entrypoint.py"
    entrypoint.write_text(
        'from robotenon.runtime.runner_cli import main\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    )
    config = make_config(tmp_path, backend="process", trials=1)
    value = json.loads(config.read_text())
    value["plugin_task_adapters"] = value["plugin_tools"] = [module_name]
    value["task_adapters"][0]["name"] = "blocking-worker"
    value["parameters"]["worker_pid_file"] = str(worker_pid_file)
    resource = "test-resource-" + uuid4().hex
    value["parameters"]["runtime"]["execution"].update(
        gpu_ids=[resource],
        resource_ids=[resource],
        shutdown_timeout_s=10,
        task_timeout_s=40,
    )
    value["tools"] = [
        {
            "name": "stop-test-model",
            "deployment": {
                "backend": "command",
                "gpu_ids": [0],
                "startup_timeout_s": 5,
                "shutdown_timeout_s": 0.1,
                "command": [
                    sys.executable,
                    str(server_script),
                    "{host}",
                    "{port}",
                    str(model_pid_file),
                ],
            },
        }
    ]
    config.write_text(json.dumps(value))
    root = Path(__file__).parents[1]
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join((str(tmp_path), str(root), os.environ.get("PYTHONPATH", ""))),
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    manager = ExperimentManager(
        project_root=tmp_path,
        config_root=".",
        output_root="out",
        entrypoint=entrypoint,
        python_executable=sys.executable,
    )
    process = None
    pids = []
    try:
        job = manager.submit(config)
        process = manager._processes[job["job_id"]]
        deadline = time.monotonic() + 15
        while not worker_pid_file.exists():
            assert process.poll() is None, Path(job["log_path"]).read_text()
            assert time.monotonic() < deadline, "worker startup deadline"
            time.sleep(0.02)
        pids = [int(path.read_text()) for path in (worker_pid_file, model_pid_file)]
        assert os.getpgid(pids[0]) == pids[0]
        # Simulate the coordinator being SIGKILLed before it can clean up.
        if prekill:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        with pytest.raises(RuntimeError, match="already reserved"):
            ResourceLease([resource]).acquire()
        if prekill:
            assert manager._refresh_job(job["job_id"])["status"] == "stop_failed"
            assert manager.get_job(job["job_id"])["status"] == "stop_failed"
        terminate = manager._terminate_process_group
        monkeypatch.setattr(
            manager,
            "_terminate_process_group",
            lambda job_id, job: terminate(job_id, job, term_timeout_s=0.05),
        )
        stopped = manager.stop_job(job["job_id"])
        assert stopped["released"] is True
        assert not set(pids) & process_table().keys()
        lease = ResourceLease([resource])
        lease.acquire()
        lease.close()
        assert manager._refresh_job(job["job_id"])["status"] == "cancelled"
    finally:
        manager.close()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_stop_failure_blocks_queue_and_cannot_be_overwritten_by_terminal_status(
    tmp_path, monkeypatch
):
    from robotenon.web.experiments import ExperimentManager

    manager = ExperimentManager(
        project_root=tmp_path, config_root=".", output_root="runs"
    )
    root = tmp_path / "runs/one"
    root.mkdir()
    (root / "status.json").write_text('{"status":"completed"}')
    manager._jobs["one"] = {
        "job_id": "one",
        "status": "stop_failed",
        "output_root": str(root),
        "status_path": str(root / "status.json"),
    }
    manager._jobs["two"] = {"job_id": "two", "status": "waiting"}
    manager._dispatch_waiting()
    assert manager._jobs["two"]["status"] == "waiting"
    assert manager._refresh_job("one")["status"] == "stop_failed"


def test_process_identity_rejects_stale_records_without_signalling(
    tmp_path, monkeypatch
):
    from robotenon.runtime import processes

    (tmp_path / "42.json").write_text(
        json.dumps({"pid": 42, "pgid": 42, "start": "old"})
    )
    monkeypatch.setattr(
        processes,
        "process_table",
        lambda: {42: {"pid": 42, "ppid": 1, "pgid": 42, "start": "new"}},
    )
    signals = []
    monkeypatch.setattr(processes.os, "kill", lambda *args: signals.append(args))
    assert processes.terminate_owned(tmp_path, timeout_s=0.01)["terminated"]
    assert signals == []


def test_failed_worker_cleanup_permanently_retires_that_worker(tmp_path):
    from robotenon.tools.base import BaseTool
    from robotenon.tools.registry import register_tool

    events = []
    name = "unreleasable-" + uuid4().hex

    @register_tool(name)
    class Tool(BaseTool):
        async def call(self, arguments, context):
            return {}

        async def start(self):
            events.append("start")

        async def end_session(self, session_id):
            raise RuntimeError("session failure")

        async def close(self):
            events.append("close")
            raise RuntimeError("resources still busy")

    config = make_config(tmp_path, trials=1)
    value = json.loads(config.read_text())
    value["tools"] = [{"name": name}]
    config.write_text(json.dumps(value))

    async def scenario():
        request = EpisodeRequest(config=config, output=tmp_path / "result.json")
        worker = WorkerRuntime(request)
        with pytest.raises(RuntimeError, match="session failure"):
            await worker.run_task(request)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="must be replaced"):
                await worker.run_task(request)
        with pytest.raises(RuntimeError, match="replacement process required"):
            await worker.close()
        assert events == ["start", "close"]

    asyncio.run(scenario())


def test_portal_fork_editor_uses_saved_snapshot_and_applies_explicit_changes(
    tmp_path, monkeypatch
):
    from robotenon.web.experiments import ExperimentManager

    config = make_config(tmp_path, trials=1)
    manager = ExperimentManager(
        project_root=tmp_path, config_root=".", output_root="runs"
    )
    monkeypatch.setattr(manager, "_dispatch_waiting", lambda: None)
    job = manager.submit(config)
    value = json.loads(config.read_text())
    value["modules"][0]["params"] = {"prefix": "disk-change"}
    config.write_text(json.dumps(value))
    draft = manager.fork_draft(job["job_id"])
    assert "disk-change" not in json.dumps(draft["config_document"])
    child = manager.submit(
        config,
        forked_from=job["job_id"],
        overrides={"parameters": {"task": {"seed": 99}}},
    )
    loaded = manager._load_job_config(child)
    assert loaded.params.resolve("task.seed") == 99
    assert loaded.modules.modules[0].params == {}


def test_linux_process_identity_uses_boot_and_start_tick_without_ps(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from robotenon.runtime import processes

    (tmp_path / "self").mkdir()
    (tmp_path / "self/stat").write_text("")
    (tmp_path / "sys/kernel/random").mkdir(parents=True)
    (tmp_path / "sys/kernel/random/boot_id").write_text("boot-id")
    for pid, state in [(101, "S"), (102, "Z")]:
        (tmp_path / str(pid)).mkdir()
        fields = [state, "1", "101"] + ["0"] * 16 + ["98765"]
        (tmp_path / str(pid) / "stat").write_text(
            f"{pid} (name with ) bracket) " + " ".join(fields)
        )
    actual_path = Path
    monkeypatch.setattr(
        processes,
        "Path",
        lambda value: (
            tmp_path / str(value).removeprefix("/proc/").removeprefix("/proc")
            if str(value).startswith("/proc")
            else actual_path(value)
        ),
    )
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda *a, **k: pytest.fail("Linux must not need ps"),
    )
    assert processes.process_table() == {
        101: {"pid": 101, "ppid": 1, "pgid": 101, "start": "boot-id:98765"}
    }


def test_default_run_ids_remain_unique_with_frozen_wall_clock(tmp_path, monkeypatch):
    from datetime import datetime
    from robotenon.runtime import experiment

    class Clock:
        @staticmethod
        def now():
            return datetime(2026, 9, 20, 12, 0, 0)

    monkeypatch.setattr(experiment, "datetime", Clock)
    config = make_config(tmp_path, trials=1)
    first = asyncio.run(
        run_experiment(ExperimentRequest(config=config, output_dir=tmp_path / "runs"))
    )
    second = asyncio.run(
        run_experiment(ExperimentRequest(config=config, output_dir=tmp_path / "runs"))
    )
    assert first["run_id"] != second["run_id"]
    assert (tmp_path / "runs" / first["run_id"] / "summary.json").is_file()
    assert (tmp_path / "runs" / second["run_id"] / "summary.json").is_file()


def test_portal_authentication_protects_read_and_control_http_endpoints():
    import http.client
    from robotenon.web.server import build_server

    calls = []
    manager = SimpleNamespace(
        shutdown_all=lambda **kwargs: calls.append(kwargs) or {"count": 0}
    )
    server = build_server(
        "127.0.0.1",
        0,
        SimpleNamespace(traces=None),
        experiment_manager=manager,
        control_token="secret-for-test",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, auth=None):
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            headers = {"Content-Type": "application/json"}
            if auth:
                headers["Authorization"] = auth
            client.request(
                method, path, body="{}" if method == "POST" else None, headers=headers
            )
            response = client.getresponse()
            result = (
                response.status,
                response.getheader("WWW-Authenticate"),
                response.read(),
            )
            return result
        finally:
            client.close()

    try:
        assert request("GET", "/")[0:2] == (
            401,
            'Basic realm="RoboTenon Portal", charset="UTF-8"',
        )
        assert request("POST", "/api/system/shutdown")[0] == 401
        assert calls == []
        assert (
            request("POST", "/api/system/shutdown", "Bearer secret-for-test")[0] == 200
        )
        assert len(calls) == 1
        assert request("GET", "/", "Bearer secret-for-test")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_resume_rejects_changed_config_without_touching_prior_result(tmp_path):
    config = make_config(tmp_path, trials=1)
    request = ExperimentRequest(
        config=config, output_dir=tmp_path / "runs", run_id="original"
    )
    asyncio.run(run_experiment(request))
    root = tmp_path / "runs/original"
    summary = (root / "summary.json").read_bytes()
    (root / ".control/resume.json").write_text('{"action":"rerun"}')
    value = json.loads(config.read_text())
    value["modules"][0]["params"] = {"prefix": "changed"}
    config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="configuration does not match"):
        asyncio.run(run_experiment(request))
    assert (root / "summary.json").read_bytes() == summary
