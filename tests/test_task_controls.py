import asyncio
import json
from pathlib import Path
from uuid import uuid4

from robotenon.infra.params import Params
from robotenon.runtime.config import TaskAdapterConfig, TaskAdaptersConfig
from robotenon.runtime.factory import build_task_adapters
from robotenon.task_adapters import BaseTaskAdapter, TaskCase, register_task_adapter
from robotenon.task_adapters.evaluator import evaluate_task_adapters, write_json


def test_stop_multiple_cases_and_rerun_in_place_preserves_other_results_and_seed(tmp_path):
    name = 'controls-' + uuid4().hex
    class Adapter(BaseTaskAdapter):
        def enumerate_cases(self, *, adapter_id, params):
            for index in range(3):
                yield TaskCase(adapter_id=adapter_id, task_id=f'task-{index}',
                               environment_id='test', index=index, seed=70+index)
    register_task_adapter(name, version='1', kind='test')(Adapter)
    adapters = build_task_adapters(TaskAdaptersConfig(task_adapters=[TaskAdapterConfig(id='sim', name=name, kind='test')]))
    root = tmp_path / 'same-id'
    control = root / '.control'
    calls, cancelled = [], []
    async def scenario():
        release = asyncio.Event()
        class Backend:
            max_workers = 2
            async def execute(self, configured, case, context):
                calls.append((case.index, case.seed))
                try:
                    if case.index == 1 or (case.index == 0 and calls.count((0,70)) == 1):
                        await release.wait()
                except asyncio.CancelledError:
                    cancelled.append(case.index)
                    raise
                return {'task_success': True, 'env_steps': 12, 'marker': len(calls)}
        async def wait_for(predicate):
            async with asyncio.timeout(5):
                while not predicate():
                    await asyncio.sleep(.01)
        kwargs = dict(adapters=adapters, params=Params({}), config_path=tmp_path/'config.json',
                      output_dir=tmp_path, run_id='same-id', run_episode=None, execution_backend=Backend())
        task = asyncio.create_task(evaluate_task_adapters(**kwargs))
        await wait_for(lambda: len(calls) == 2)
        targets = [{'adapter_id':'sim','global_index':index} for index in (0,2)]
        write_json(control/'command-1.json', {'action':'stop','tasks':targets})
        await wait_for(lambda: (root/'task-2/episode_000_seed_72.json').exists() and
                       json.loads((root/'task-2/episode_000_seed_72.json').read_text())['status']=='cancelled')
        assert cancelled == [0]
        assert (2,72) not in calls
        assert not task.done()
        write_json(control/'command-2.json', {'action':'rerun','tasks':[targets[0]]})
        await wait_for(lambda: calls.count((0,70)) == 2)
        assert not task.done(), 'unselected running task remains alive'
        release.set()
        summary = await task
        assert summary['successes'] == 2
        assert summary['num_cancelled'] == 1
        assert summary['num_errors'] == 0
        assert len(summary['task_adapters']['sim']['episodes']) == 3
        other = root/'task-1/episode_000_seed_71.json'
        previous = other.read_bytes()
        write_json(control/'resume.json', {'action':'rerun','tasks':[targets[0]]})
        summary = await evaluate_task_adapters(**kwargs)
        assert other.read_bytes() == previous
        assert calls.count((0,70)) == 3
        assert calls.count((1,71)) == 1
        assert summary['successes'] == 2
        assert len(list(tmp_path.glob('same-id'))) == 1
        assert not (control/'ready.json').exists()
    asyncio.run(scenario())


def test_clear_case_artifacts_removes_previous_media_and_attempts_only(tmp_path):
    from robotenon.task_adapters.evaluator import clear_case_artifacts
    path = tmp_path / 'episode_000_seed_70.json'
    write_json(path, {'status': 'completed'})
    old = path.with_suffix('') / 'attempts' / 'old' / 'resources' / 'frame.npy'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'old frame')
    trace = path.with_suffix('.trace')
    trace.mkdir()
    (trace / 'trace.jsonl').write_text('old trace')
    for artifact in (path.with_suffix('.mp4'), path.with_suffix('.memory.json'),
                     path.parent / '.live' / path.name):
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text('old data')
    other = tmp_path / 'episode_001_seed_71.mp4'
    other.write_bytes(b'keep')
    clear_case_artifacts(path)
    assert not old.exists()
    assert not trace.exists()
    assert not path.with_suffix('.mp4').exists()
    assert not path.with_suffix('.memory.json').exists()
    assert not (path.parent / '.live' / path.name).exists()
    assert other.read_bytes() == b'keep'
    clear_case_artifacts(path)
