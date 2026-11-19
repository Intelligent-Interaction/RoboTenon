"""Independent, bounded audit probes: passing confirms a documented defect.
Only temporary artifacts, fake drivers and local subprocesses are used.
Run from the repo root: python -m pytest -o addopts= -q -s docs/audit-2026-09-20-independent/reproduce.py
"""
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "robosys").is_dir())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from robosys.bodies.real_robot_body import RealRobotBody
from robosys.infra.params import Params
from robosys.modules.verify import VerifyModule
from robosys.runtime.safety import SafetyStateStore
from robosys.schemas import Action, ActionChunk
from robosys.services.body import InProcessBodyService


def test_cancelled_body_never_calls_stop():
    async def probe():
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        stops = []
        def execute(action):
            entered.set()
            release.wait(2)
            finished.set()
        def stop(reason):
            stops.append(reason)
            release.set()
        body = InProcessBodyService(execute, stop_executor=stop, safety_state=SafetyStateStore())
        session = uuid4()
        task = asyncio.create_task(body.execute(ActionChunk(session_id=session, actions=[
            Action(session_id=session, timeout_ms=1000)
        ], max_duration_ms=2000)))
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            task.cancel()
            result = await task
            await body.close()
            assert result.interrupted
            assert stops == []
            assert not finished.is_set()
            assert body._sync_driver_busy.is_set()
            print('CANCEL', {'status': result.status.value, 'stop_calls': len(stops), 'driver_still_running': True})
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 1)
            await body.close()
    asyncio.run(probe())


def test_real_robot_sync_driver_bypasses_timeout():
    async def probe():
        events = []
        class Driver:
            def execute(self, action):
                events.append('driver_started')
                time.sleep(.12)
                events.append('driver_finished')
            def hold(self, reason):
                events.append('stop')
        body = RealRobotBody(driver=Driver(), safety_state=SafetyStateStore())
        session = uuid4()
        async def tick():
            await asyncio.sleep(.02)
            events.append('tick')
        timer = asyncio.create_task(tick())
        try:
            start = time.monotonic()
            result = await body.execute(ActionChunk(session_id=session, actions=[
                Action(session_id=session, timeout_ms=25)
            ], max_duration_ms=2000))
            elapsed = time.monotonic() - start
            await timer
            assert result.action_results[0].status == 'succeeded'
            assert events.index('tick') > events.index('driver_finished')
            assert elapsed >= .12
            print('SYNC_DRIVER', {'timeout_ms': 25, 'elapsed_ms': round(elapsed*1000), 'status': result.status.value, 'events': events})
        finally:
            await body.close()
    asyncio.run(probe())


def test_verifier_false_string_is_committed_as_success():
    async def probe():
        async def call_tool(*args):
            return SimpleNamespace(ok=True, output={'subtask_success': 'false'}, metadata={})
        ctx = SimpleNamespace(feedback=None,
            inputs={'needs_plan': True, 'instruction': 'pick'}, values={},
            params=Params({'model': {'hlp_tool': 'fake'}, 'task': {'task_id': 'pick'}}), call_tool=call_tool)
        result = await VerifyModule().run(ctx)
        assert result['subtask_success'] is True
        assert result['stm']['completed'] == ['pick']
        assert result['stm']['remaining'] == []
        print('VERIFIER', {'input': 'false', 'success': result['subtask_success'], 'completed': result['stm']['completed']})
    asyncio.run(probe())


def test_idempotency_key_does_not_bind_payload():
    async def probe():
        calls = []
        async def execute(action):
            calls.append(action.payload)
        body = InProcessBodyService(execute, safety_state=SafetyStateStore())
        session = uuid4()
        try:
            for value in (1, 2):
                result = await body.execute(ActionChunk(session_id=session, actions=[
                    Action(session_id=session, idempotency_key='same', payload={'target': value})
                ]))
                assert result.action_results[0].status == 'succeeded'
            assert calls == [{'target': 1}]
            print('IDEMPOTENCY', {'executed': calls, 'second_status': result.status.value, 'second_executed': result.executed_actions})
        finally:
            await body.close()
    asyncio.run(probe())


def fake_config(path):
    value = {
        'modules': [{'name': 'echo', 'id': 'echo', 'params': {'prefix': 'before'}}],
        'tools': [], 'monitors': [], 'bodies': [],
        'environments': [{'name': 'fake', 'id': 'env'}],
        'task_adapters': [{'name': 'single-task', 'params': {'num_trials': 1}}],
        'graphs': [{'id': 'test', 'entry': 'reset',
            'outputs': {'success': {'source': 'nodes.echo.outputs.success', 'default': False},
                        'marker': 'nodes.echo.outputs.prefix'},
            'nodes': [{'id': 'reset', 'type': 'system', 'operation': 'env.reset', 'next': [{'node': 'echo'}]},
                      {'id': 'echo', 'module': 'echo', 'predecessors': ['reset']}]}],
        'parameters': {'task': {'task_id': 'test', 'env_id': 'fake', 'seed': 17},
            'runtime': {'environment_id': 'env', 'graph_id': 'test', 'monitor_enabled': False,
                        'execution': {'backend': 'in_process', 'memory_mode': 'disabled'}}},
        'observation_cache': {'enabled': False}, 'safety_state': {'backend': 'memory'}
    }
    path.write_text(json.dumps(value))
    return value


def test_resident_worker_mixes_old_snapshot_and_new_module_config(tmp_path):
    from robosys.runtime.runner import EpisodeRequest
    from robosys.runtime.worker import WorkerRuntime
    async def probe():
        path = tmp_path / 'config.json'
        value = fake_config(path)
        request = EpisodeRequest(config=path, output=tmp_path/'result.json', memory_enabled=False)
        worker = WorkerRuntime(request)
        try:
            first = await worker.run_task(request)
            value['modules'][0]['params']['prefix'] = 'after'
            path.write_text(json.dumps(value))
            second = await worker.run_task(request)
            assert first['outputs']['marker'] == 'before'
            assert second['outputs']['marker'] == 'after'
            assert worker.loaded.modules.modules[0].params['prefix'] == 'before'
            print('CONFIG_DRIFT', {'stored_module_prefix': 'before', 'first': first['outputs']['marker'], 'second': second['outputs']['marker']})
        finally:
            await worker.close()
    asyncio.run(probe())


def test_duplicate_case_key_loses_a_running_task(tmp_path):
    from robosys.task_adapters.base import BaseTaskAdapter, ConfiguredTaskAdapter, TaskCase
    from robosys.task_adapters.evaluator import evaluate_task_adapters
    async def probe():
        tasks, entered, finished = [], [], []
        gate = asyncio.Event()
        class Adapter(BaseTaskAdapter):
            def enumerate_cases(self, *, adapter_id, params):
                for index, seed in enumerate((10, 20)):
                    yield TaskCase(adapter_id=adapter_id, task_id='same-task', environment_id='fake', index=index, episode=0, seed=seed)
        class Backend:
            max_workers = 2
            async def execute(self, configured, case, context):
                tasks.append(asyncio.current_task())
                entered.append(case.index)
                if case.index == 0:
                    await gate.wait()
                finished.append(case.index)
                return {'task_success': True}
        configured = ConfiguredTaskAdapter('adapter', SimpleNamespace(name='fixture'), Adapter())
        try:
            result = await asyncio.wait_for(evaluate_task_adapters(config_path=tmp_path/'config.json',
                params=Params({}), adapters=[configured], output_dir=tmp_path, run_id='duplicate-keys',
                run_episode=None, execution_backend=Backend(), options={'memory_mode': 'disabled'}), timeout=1)
            status = json.loads((tmp_path/'duplicate-keys/status.json').read_text())
            assert entered == [0, 1]
            assert finished == [1]
            assert status['status'] == 'completed'
            assert status['completed_episodes'] == 1 and status['total_episodes'] == 2
            assert not tasks[0].done()
            print('CASE_KEYS', {'entered': entered, 'finished': finished, 'status': status['status'],
                                'completed': status['completed_episodes'], 'total': status['total_episodes'], 'task_0_still_running': True})
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(probe())


def test_control_boundary_ignores_supplied_token():
    from email.message import Message
    from robosys.web.access import PortalAccess
    headers = Message()
    headers['Host'] = '127.0.0.1:8765'
    headers['Content-Type'] = 'application/json'
    access = PortalAccess('0.0.0.0', control=True, token='configured-secret')
    assert access.check(headers, write=True, control=True) is None
    assert access.token is None
    print('PORTAL_ACCESS', {'bind': '0.0.0.0', 'authorization_sent': False, 'control_accepted': True})


def test_portal_force_stop_reports_released_while_worker_is_alive(tmp_path, monkeypatch):
    import signal
    import subprocess
    from robosys.web.experiments import ExperimentManager
    worker_pid_file = tmp_path / 'worker.pid'
    module_name = 'audit_worker_' + uuid4().hex
    (tmp_path / (module_name + '.py')).write_text('''
import os, time
from pathlib import Path
from robosys.task_adapters import BaseTaskAdapter, register_task_adapter
from robosys.task_adapters.single import SingleTaskAdapter
@register_task_adapter("audit-blocking-worker")
class BlockingAdapter(SingleTaskAdapter):
    async def run_case(self, case, context):
        Path(context.params.resolve("audit_worker_pid_file")).write_text(str(os.getpid()))
        time.sleep(30)
        return {"task_success": False}
''')
    entrypoint = tmp_path / 'entrypoint.py'
    entrypoint.write_text('from robosys.runtime.runner_cli import main\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
    config = tmp_path / 'config.json'
    value = fake_config(config)
    value['plugin_task_adapters'] = [module_name]
    value['task_adapters'][0]['name'] = 'audit-blocking-worker'
    value['parameters']['audit_worker_pid_file'] = str(worker_pid_file)
    value['parameters']['runtime']['execution'].update(backend='process', startup_timeout_s=10,
        task_timeout_s=40, shutdown_timeout_s=10)
    config.write_text(json.dumps(value))
    root = ROOT
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join((str(tmp_path), str(root), os.environ.get('PYTHONPATH', ''))))
    manager = ExperimentManager(project_root=tmp_path, config_root='.', output_root='out',
                                entrypoint=entrypoint, python_executable=sys.executable)
    worker_pid = None
    process = None
    try:
        job = manager.submit(config)
        process = manager._processes[job['job_id']]
        deadline = time.monotonic() + 15
        while not worker_pid_file.exists():
            if process.poll() is not None:
                pytest.fail('Coordinator exited: ' + Path(job['log_path']).read_text())
            assert time.monotonic() < deadline, 'worker startup deadline'
            time.sleep(.02)
        worker_pid = int(worker_pid_file.read_text())
        assert os.getpgid(worker_pid) == worker_pid
        stopped = manager.stop_job(job['job_id'])
        state = subprocess.check_output(['ps', '-o', 'state=', '-p', str(worker_pid)], text=True).strip()
        assert stopped['released'] is True
        assert process.poll() is not None
        assert state and 'Z' not in state
        print('PORTAL_STOP', {'released': stopped['released'], 'coordinator_exit': process.returncode,
                              'worker_still_alive': True, 'worker_state': state})
    finally:
        manager.close()
        if worker_pid is not None:
            try: os.killpg(worker_pid, signal.SIGKILL)
            except ProcessLookupError: pass
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_idle_ingest_client_blocks_shutdown():
    import tempfile
    from robosys.infra.ingest_server import IngestServer
    async def probe():
        class Store:
            def ingest_batch(self, batch): pass
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='roboclaw-audit-') as directory:
            server = await IngestServer(Path(directory)/'ingest.sock', Store()).start()
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            close = None
            try:
                await asyncio.sleep(.02)
                close = asyncio.create_task(server.close())
                await asyncio.sleep(.15)
                assert not close.done()
                print('INGEST_CLOSE', {'idle_client': True, 'close_completed_after_150ms': False})
            finally:
                writer.close()
                await writer.wait_closed()
                if close is not None: await asyncio.wait_for(close, 1)
                else: await server.close()
    asyncio.run(probe())


def test_inprocess_tool_is_reused_after_session_cleanup_failure(tmp_path):
    from robosys.runtime.experiment import ExperimentRequest, run_experiment
    from robosys.tools.base import BaseTool
    from robosys.tools.registry import register_tool
    events = []
    class PoisonTool(BaseTool):
        async def call(self, arguments, context): return {}
        async def start(self): events.append('start')
        async def end_session(self, session_id):
            events.append('cleanup_failed')
            raise RuntimeError('session cleanup failed')
        async def close(self): events.append('close')
    name = 'audit-poison-' + uuid4().hex
    register_tool(name)(PoisonTool)
    config = tmp_path/'config.json'
    value = fake_config(config)
    value['tools'] = [{'name': name}]
    value['task_adapters'][0]['params']['num_trials'] = 2
    config.write_text(json.dumps(value))
    result = asyncio.run(run_experiment(ExperimentRequest(config=config, output_dir=tmp_path/'out', run_id='poison')))
    assert result['num_errors'] == 2
    assert events == ['start', 'cleanup_failed', 'cleanup_failed', 'close']
    print('TOOL_REUSE', {'events': events, 'errors': result['num_errors']})


def test_new_run_can_overwrite_existing_run_artifacts(tmp_path):
    from robosys.runtime.experiment import ExperimentRequest, run_experiment
    config = tmp_path/'config.json'
    fake_config(config)
    request = ExperimentRequest(config=config, output_dir=tmp_path/'out', run_id='same-run')
    asyncio.run(run_experiment(request))
    episode = next((tmp_path/'out/same-run/test').glob('episode_*.json'))
    old_attempt = json.loads(episode.read_text())['result']['identity']['attempt_id']
    asyncio.run(run_experiment(request))
    new_attempt = json.loads(episode.read_text())['result']['identity']['attempt_id']
    assert old_attempt != new_attempt
    print('RUN_OVERWRITE', {'same_run_id': True, 'overwrite_rejected': False, 'attempt_changed': True})
