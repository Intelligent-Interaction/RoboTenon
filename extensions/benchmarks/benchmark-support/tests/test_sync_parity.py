"""Differential test against the pinned, unmodified upstream runner source."""
import ast
import asyncio
import itertools
import json
from pathlib import Path
from typing import Any
from copy import deepcopy
import pytest

from robotenon.runtime.experiment import ExperimentRequest, run_experiment
from robotenon_benchmark_support.configure import application_config, write_application_config
from robotenon_benchmark_support.integration import task_manifest
from robotenon_benchmark_support.execution import NativePool
from parity_fixtures import StatefulBenchmark, VLA, EVENTS, INSTANCES


def reference_runner():
    # Only remove the base class / annotation import dependencies. The actual
    # run_episode AST is executed unchanged from the checked-in upstream source.
    source = Path(__file__).parent/'reference/sync_runner.py.txt'
    node = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef))
    node.bases = []
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    scope = {'itertools':itertools, 'Any':Any}
    exec(compile(module, str(source), 'exec'), scope)
    return scope['SyncEpisodeRunner']()


@pytest.mark.parametrize('done_after,horizon', [(1,3),(2,3),(99,3)])
@pytest.mark.parametrize('throughput', [False, True])
def test_graph_matches_reference_across_tasks_and_episodes(tmp_path, done_after, horizon, throughput):
    async def run():
        EVENTS.clear();INSTANCES.clear()
        ref = StatefulBenchmark(done_after)
        tasks = ref.get_tasks()
        for task in tasks:
            for ep in range(3):
                await reference_runner().run_episode(ref, {**task,'episode_idx':ep%2 if throughput else ep},
                                                       AsyncVLA(), max_steps=horizon)
        expected = deepcopy(EVENTS)
        assert ref.task_reads == 1
        ref.cleanup()
        manifest = task_manifest(StatefulBenchmark(done_after))
        EVENTS.clear();INSTANCES.clear()
        cfg = application_config({'benchmark_id':'parity_benchmark','params':{'done_after':done_after},
                                  'episodes_per_task':3,'max_steps':horizon,'throughput_mode':throughput},
                                  policy_factory='parity_fixtures:create', policy_interface='vla')
        for key in ('plugin_environments','plugin_bodies','plugin_task_adapters'):
            cfg[key] = ['parity_fixtures']
        cfg['task_adapters'][0]['params']['manifest'] = manifest
        cfg['safety_state'] = {'backend':'memory','require_shared':False}
        path = tmp_path/'config.json';write_application_config(cfg, path)
        report = await run_experiment(ExperimentRequest(config=path, output_dir=tmp_path/'runs', run_id='parity'))
        assert report['num_errors'] == 0, report
        assert report['num_episodes'] == 6
        episode_paths = [path for path in (tmp_path/'runs'/'parity').rglob('episode_*.json')
                         if '.live' not in path.parts]
        assert len(episode_paths) == 6
        for episode_path in episode_paths:
            episode = json.loads(episode_path.read_text())
            assert episode['result']['outputs']['native_dimensions'] == {
                'task': episode['task_id']}
        assert EVENTS == expected
        assert len(INSTANCES) == 1
        assert INSTANCES[0].task_reads == 1
        assert INSTANCES[0].cleaned == 1
    asyncio.run(run())


class AsyncVLA(VLA):
    async def start_episode(self, info):return super().start_episode(info)
    async def act(self, obs):return super().act(obs)
    async def end_episode(self, result):return super().end_episode(result)


def test_pool_rejects_overlap_and_switches_parameter_sets_without_retaining_runtimes():
    pool=NativePool()
    native,_=pool.acquire('test',StatefulBenchmark,{},'gpu')
    with pytest.raises(RuntimeError,match='interleave'):
        pool.acquire('test',StatefulBenchmark,{},'gpu')
    pool.release('test')
    other,_=pool.acquire('test',StatefulBenchmark,{'done_after':5},'gpu')
    assert native.cleaned == 1
    pool.release('test')
    reused,_=pool.acquire('test',StatefulBenchmark,{},'gpu')
    assert reused is native
    assert other.cleaned == 1
    pool.release('test')
    pool.close();pool.close()
    assert native.cleaned == 2
    assert other.cleaned == 1


def test_observation_parameter_merge_matches_reference_rules(monkeypatch, caplog):
    import parity_fixtures
    from robotenon_benchmark_support.observation_params import merge_observation_params
    class Child(StatefulBenchmark):
        def __init__(self, camera=False, **kwargs):pass
    monkeypatch.setattr(parity_fixtures, 'OBSERVATION_PARAMS',
                        {'done_after':7,'camera':True,'unsupported':1}, raising=False)
    merged=merge_observation_params(Child, {'done_after':3}, 'parity_fixtures:create')
    assert merged=={'done_after':3,'camera':True}
    assert 'unsupported' in caplog.text


def test_native_pool_cleanup_after_graph_failure(tmp_path, monkeypatch):
    import parity_fixtures
    class BrokenVLA(VLA):
        def act(self, observation):raise RuntimeError('inference failed')
        def end_episode(self, result):
            assert result['status']=='aborted'
    monkeypatch.setattr(parity_fixtures, 'VLA', BrokenVLA)
    manifest=task_manifest(StatefulBenchmark())
    INSTANCES.clear()
    cfg=application_config({'benchmark_id':'parity_benchmark','max_tasks':1,'max_steps':3},
                           policy_factory='parity_fixtures:create')
    for key in ('plugin_environments','plugin_bodies','plugin_task_adapters'):cfg[key]=['parity_fixtures']
    cfg['task_adapters'][0]['params']['manifest']=manifest
    cfg['safety_state']={'backend':'memory','require_shared':False}
    path=tmp_path/'broken.json';write_application_config(cfg,path)
    report=asyncio.run(run_experiment(ExperimentRequest(config=path,output_dir=tmp_path/'runs',run_id='broken')))
    assert report['num_errors']==1
    assert len(INSTANCES)==1 and INSTANCES[0].cleaned==1
