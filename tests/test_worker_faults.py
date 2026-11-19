import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from test_execution_backends import make_config

from robotenon.runtime.execution import ResourceLease
from robotenon.runtime.experiment import ExperimentRequest, run_experiment


def test_native_crash_error_includes_signal_and_fault_site(tmp_path):
    from robotenon.runtime.parallel import ProcessSlot

    slot = ProcessSlot(SimpleNamespace(output=tmp_path / "result.json"), None, None)
    slot.process = SimpleNamespace(pid=123, exitcode=-6)
    slot.worker_id = "test-worker"
    log = tmp_path / "workers/test-worker/worker-fault.log"
    log.parent.mkdir(parents=True)
    log.write_text('Fatal Python error: Aborted\n\nCurrent thread 0x123:\n'
                   '  File "/provider/binding_utils.py", line 174 in read_pixels\n'
                   'Thread 0x456:\n  File "/unrelated.py", line 1 in wait\n')
    error = str(slot._lost_error("worker connection closed"))
    assert "signal=SIGABRT" in error
    assert f"fault_log={log}" in error
    assert "binding_utils.py" in error and "read_pixels" in error
    assert "unrelated.py" not in error


def test_worker_exit_without_fault_log_keeps_exit_code(tmp_path):
    from robotenon.runtime.parallel import ProcessSlot

    slot = ProcessSlot(SimpleNamespace(output=tmp_path / "result.json"), None, None)
    slot.process = SimpleNamespace(pid=123, exitcode=17)
    slot.worker_id = "test-worker"
    assert "exitcode=17" in str(slot._lost_error("worker exited"))


def test_crashed_episode_preserves_only_current_run_progress(tmp_path):
    from robotenon.task_adapters.evaluator import _last_known_progress

    episode = tmp_path / "episode.json"
    live = tmp_path / ".live/episode.json"
    live.parent.mkdir()
    live.write_text(json.dumps({
        "identity": {"run_id": "current"},
        "updated_at": "2026-09-19T02:43:43Z",
        "context": {"current_node": "body_execute", "values": {"env_steps": 112}},
    }))
    progress = _last_known_progress(episode, "current")
    assert progress["env_steps"] == 112
    assert progress["env_steps_partial"] is True
    assert progress["last_active_node"] == "body_execute"
    assert _last_known_progress(episode, "another-run") == {}
    live.write_text("{")
    assert _last_known_progress(episode, "current") == {}


def test_worker_thread_environment_is_scoped(monkeypatch):
    from robotenon.runtime.parallel import _worker_thread_environment

    monkeypatch.setenv("OMP_NUM_THREADS", "96")
    monkeypatch.delenv("MKL_NUM_THREADS", raising=False)
    with _worker_thread_environment(2):
        assert os.environ["OMP_NUM_THREADS"] == "2"
        assert os.environ["MKL_NUM_THREADS"] == "2"
    assert os.environ["OMP_NUM_THREADS"] == "96"
    assert "MKL_NUM_THREADS" not in os.environ


def test_worker_devices_are_bound_before_spawn_and_restored(monkeypatch):
    from robotenon.runtime.parallel import _worker_thread_environment

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1,2,3,4,5,6,7")
    monkeypatch.delenv("MUJOCO_EGL_DEVICE_ID", raising=False)
    with _worker_thread_environment(2, gpu_id="4", egl_device_id="4"):
        assert os.environ["CUDA_VISIBLE_DEVICES"] == "4"
        assert os.environ["MUJOCO_EGL_DEVICE_ID"] == "4"
    assert os.environ["CUDA_VISIBLE_DEVICES"] == "0,1,2,3,4,5,6,7"
    assert "MUJOCO_EGL_DEVICE_ID" not in os.environ



PLUGIN = """
import asyncio
import os
from pathlib import Path
from robotenon.task_adapters import BaseTaskAdapter, register_task_adapter
from robotenon.task_adapters.single import SingleTaskAdapter
from robotenon.tools.base import BaseTool
from robotenon.tools.registry import register_tool

@register_task_adapter("fault-case")
class FaultAdapter(SingleTaskAdapter):
    async def run_case(self, case, context):
        marker = context.output_root / "entered"
        mode = context.params.resolve("test_mode")
        if mode == "render_crash" and os.environ.get("MUJOCO_GL") != "osmesa":
            namespace = {"os": os}
            exec(compile("def read_pixels(): os.abort()", "/provider/binding_utils.py", "exec"), namespace)
            namespace["read_pixels"]()
        if mode == "graph_error":
            return {"graph_status": "failed", "error": "invalid model response", "env_steps": 32}
        if mode == "graph_error_once" and not marker.exists():
            marker.write_text("failed")
            return {"graph_status": "failed", "error": "transient model timeout", "env_steps": 32}
        if mode == "crash" and not marker.exists():
            marker.write_text("crashed")
            os._exit(17)
        marker.write_text("running")
        if mode == "wait":
            await asyncio.sleep(60)
        return await super().run_case(case, context)

@register_tool("lifecycle-tool")
class LifecycleTool(BaseTool):
    def __init__(self, *, log):
        self.log = Path(log)
    def record(self, text):
        with self.log.open("a") as f:
            f.write(text + "\\n")
    async def start(self):
        self.record("start")
    async def close(self):
        self.record("close")
    async def end_session(self, session_id):
        self.record("session:" + session_id)
    async def call(self, arguments, context):
        raise AssertionError("test graph should not invoke tool")
"""


def plugin_config(
    tmp_path, monkeypatch, *, mode=None, backend="process", trials=1, retries=0
):
    (tmp_path / "runtime_fault_fixture.py").write_text(PLUGIN)
    monkeypatch.syspath_prepend(str(tmp_path))
    path = make_config(tmp_path, backend=backend, trials=trials)
    value = json.loads(path.read_text())
    value["plugin_task_adapters"] = ["runtime_fault_fixture"]
    value["plugin_tools"] = ["runtime_fault_fixture"]
    value["task_adapters"][0]["name"] = "fault-case"
    value["tools"] = [
        {"name": "lifecycle-tool", "params": {"log": str(tmp_path / "lifecycle.log")}}
    ]
    value["parameters"]["test_mode"] = mode
    value["parameters"]["runtime"]["execution"]["max_retries"] = retries
    path.write_text(json.dumps(value))
    return path


def test_worker_crash_retries_with_new_attempt(tmp_path, monkeypatch):
    config = plugin_config(tmp_path, monkeypatch, mode="crash", retries=1)
    result = asyncio.run(
        run_experiment(
            ExperimentRequest(config=config, output_dir=tmp_path, run_id="run")
        )
    )
    assert result["num_errors"] == 0
    attempts = [
        json.loads(p.read_text())
        for p in (tmp_path / "run").glob("**/attempts/*/status.json")
    ]
    assert sorted(a["status"] for a in attempts) == ["lost", "succeeded"]
    assert len({a["identity"]["attempt_id"] for a in attempts}) == 2
    assert len({a["identity"]["episode_id"] for a in attempts}) == 1
    lost = next(a for a in attempts if a["status"] == "lost")
    assert "17" in lost["error"]


def test_returned_graph_failure_is_not_a_successful_attempt(tmp_path, monkeypatch):
    config = plugin_config(tmp_path, monkeypatch, mode="graph_error")
    result = asyncio.run(run_experiment(
        ExperimentRequest(config=config, output_dir=tmp_path, run_id="run")
    ))
    assert result["num_errors"] == 1
    assert result["num_completed"] == 0
    attempts = [json.loads(p.read_text())
                for p in (tmp_path / "run").glob("**/attempts/*/status.json")]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error"] == "invalid model response"


def test_returned_graph_failure_uses_retry_budget(tmp_path, monkeypatch):
    config = plugin_config(
        tmp_path, monkeypatch, mode="graph_error_once", retries=1
    )
    result = asyncio.run(
        run_experiment(
            ExperimentRequest(config=config, output_dir=tmp_path, run_id="run")
        )
    )
    assert result["num_errors"] == 0
    assert result["num_completed"] == 1
    attempts = [
        json.loads(p.read_text())
        for p in (tmp_path / "run").glob("**/attempts/*/status.json")
    ]
    assert sorted(item["status"] for item in attempts) == ["failed", "succeeded"]
    assert len({item["identity"]["attempt_id"] for item in attempts}) == 2
    assert len({item["identity"]["episode_id"] for item in attempts}) == 1


def test_cancellation_stops_workers_and_shared_services(tmp_path, monkeypatch):
    config = plugin_config(tmp_path, monkeypatch, mode="wait")

    async def scenario():
        task = asyncio.create_task(
            run_experiment(
                ExperimentRequest(config=config, output_dir=tmp_path, run_id="run")
            )
        )
        try:
            async with asyncio.timeout(10):
                while not (tmp_path / "run/entered").exists():
                    if task.done():
                        await task
                    await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert (
        json.loads((tmp_path / "run/status.json").read_text())["status"] == "cancelled"
    )
    assert "close" in (tmp_path / "lifecycle.log").read_text().splitlines()


def test_resident_tool_starts_once_but_releases_each_session(tmp_path, monkeypatch):
    config = plugin_config(tmp_path, monkeypatch, trials=3)
    result = asyncio.run(
        run_experiment(
            ExperimentRequest(config=config, output_dir=tmp_path, run_id="run")
        )
    )
    assert result["num_errors"] == 0
    events = (tmp_path / "lifecycle.log").read_text().splitlines()
    assert events.count("start") == events.count("close") == 1
    sessions = [line for line in events if line.startswith("session:")]
    assert len(sessions) == len(set(sessions)) == 3


def test_resource_lease_excludes_other_launchers():
    first, second = ResourceLease(["test-device"]), ResourceLease(["test-device"])
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already reserved"):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


def test_render_retry_classifier_does_not_retry_unrelated_native_errors():
    from robotenon.runtime.parallel import is_native_render_crash
    assert is_native_render_crash('signal=SIGABRT, crash_site=File "/robosuite/utils/binding_utils.py", line 174 in read_pixels')
    assert not is_native_render_crash('signal=SIGABRT, crash_site=File "/other.py", line 174 in allocate')
    assert not is_native_render_crash('worker exceeded execution deadline')


def test_software_render_spawn_environment_is_scoped(monkeypatch):
    from robotenon.runtime.parallel import _worker_thread_environment
    monkeypatch.setenv('MUJOCO_GL', 'egl')
    monkeypatch.setenv('PYOPENGL_PLATFORM', 'egl')
    monkeypatch.delenv('LP_NUM_THREADS', raising=False)
    with _worker_thread_environment(2, gpu_id='3', render_backend='osmesa'):
        assert os.environ['MUJOCO_GL'] == 'osmesa'
        assert os.environ['PYOPENGL_PLATFORM'] == 'osmesa'
        assert os.environ['CUDA_VISIBLE_DEVICES'] == '3'
        assert os.environ['LP_NUM_THREADS'] == '2'
    assert os.environ['MUJOCO_GL'] == 'egl'
    assert os.environ['PYOPENGL_PLATFORM'] == 'egl'
    assert 'LP_NUM_THREADS' not in os.environ


def test_render_crash_switches_only_retry_then_releases_software_worker(tmp_path, monkeypatch):
    from dataclasses import replace
    from robotenon.runtime.parallel import PersistentExecutionBackend, ProcessSlot, WorkerLost
    from robotenon.runtime.execution import ExecutionOptions
    from robotenon.runtime.runner import EpisodeRequest
    from robotenon.runtime.observability import ExecutionIdentity
    from robotenon.infra.params import Params
    from types import SimpleNamespace
    starts, closed = [], []
    async def start(self):
        starts.append(self.render_backend)
        self.worker_id = 'w' + str(len(starts))
        self.connection = SimpleNamespace(send=lambda value: None)
    async def receive(self, timeout):
        if self.render_backend is None:
            raise WorkerLost('signal=SIGABRT, crash_site=File "/robosuite/utils/binding_utils.py", line 174 in read_pixels')
        status = next(p for p in (tmp_path / 'episode/attempts').glob('*/status.json')
                      if json.loads(p.read_text())['status'] == 'running')
        attempt = json.loads(status.read_text())['identity']['attempt_id']
        return 'result', attempt, {'graph_status': 'completed', 'task_success': False, 'reason': 'horizon'}
    async def close(self):
        closed.append(self.render_backend)
    monkeypatch.setattr(ProcessSlot, 'start', start)
    monkeypatch.setattr(ProcessSlot, 'receive', receive)
    monkeypatch.setattr(ProcessSlot, 'close', close)
    identity = ExecutionIdentity(experiment_id='test', run_id='run', episode_id='episode', attempt_id='base', task_id='task', adapter_id='a')
    request = EpisodeRequest(output=tmp_path / 'episode.json', identity=identity)
    backend = PersistentExecutionBackend(ExecutionOptions(max_retries=1, render_retry_backend='osmesa'), request,
        lambda *args: request)
    configured = SimpleNamespace(config=SimpleNamespace(model_dump=lambda **kwargs: {}))
    context = SimpleNamespace(case_output=tmp_path/'episode.json', case_trace_dir=None, memory_path=None,
                              output_root=tmp_path, options={}, params=Params({}))
    result = asyncio.run(backend.execute(configured, object(), context))
    assert starts == [None, 'osmesa']
    assert result['task_success'] is False  # completing a retry is not task success
    assert backend.slots[0].render_backend is None
    assert 'osmesa' in closed
    attempts = [json.loads(p.read_text()) for p in (tmp_path/'episode/attempts').glob('*/status.json')]
    assert len(attempts) == 2
    lost = next(a for a in attempts if a['status'] == 'lost')
    assert lost['retry_render_backend'] == 'osmesa'
    assert next(a for a in attempts if a['status'] == 'succeeded')['render_backend'] == 'osmesa'


def test_native_abort_restarts_spawn_worker_with_software_rendering(tmp_path, monkeypatch):
    import sys
    if sys.platform != "linux":
        pytest.skip("server/Linux native crash and preload integration")
    config = plugin_config(tmp_path, monkeypatch, mode="render_crash", retries=1)
    raw = json.loads(config.read_text())
    raw['parameters']['runtime']['execution']['render_retry_backend'] = 'osmesa'
    config.write_text(json.dumps(raw))
    monkeypatch.setenv('MUJOCO_GL', 'egl')
    monkeypatch.setenv('PYOPENGL_PLATFORM', 'egl')
    result = asyncio.run(run_experiment(ExperimentRequest(config=config, output_dir=tmp_path, run_id='run')))
    assert result['num_errors'] == 0
    assert result['num_completed'] == 1
    attempts = [json.loads(p.read_text()) for p in (tmp_path/'run').glob('**/attempts/*/status.json')]
    assert len(attempts) == 2
    lost = next(a for a in attempts if a['status'] == 'lost')
    assert 'SIGABRT' in lost['error']
    assert lost['retry_render_backend'] == 'osmesa'
    assert next(a for a in attempts if a['status'] == 'succeeded')['render_backend'] == 'osmesa'
    assert os.environ['MUJOCO_GL'] == 'egl'
