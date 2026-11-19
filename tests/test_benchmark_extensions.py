from __future__ import annotations

import ast
import asyncio
import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT/'docs/benchmark-catalog.json').read_text())
for entry in CATALOG:
    sys.path.insert(0,str(ROOT/entry['path']/'src'))

from benchmark_fixtures import ContractBenchmark, Environment, Tasks
from robotenon_benchmark_support.configure import (
    application_config as extension_application_config,
)
from robotenon_benchmark_support.results import aggregate, from_episode_files

from robotenon.benchmarks.codec import pack_observation, unpack_observation
from robotenon.benchmarks.configure import application_config
from robotenon.benchmarks.integration import task_manifest
from robotenon.benchmarks.modules import validate_native_action
from robotenon.benchmarks.recording import EpisodeRecorder
from robotenon.infra.params import Params
from robotenon.runtime.config import load_application_config
from robotenon.runtime.runner import EpisodeRequest, run_episode
from robotenon.schemas import Action, ActionType, SessionSpec


@pytest.mark.parametrize('entry', CATALOG, ids=lambda e:e['id'])
def test_every_extension_is_independent_and_registers(entry):
    plugin=importlib.import_module(f"robotenon_{entry['id']}.benchmark_plugin")
    assert plugin.Environment.benchmark_id == entry['registry']
    assert plugin.Tasks.benchmark_id == entry['registry']
    root=ROOT/entry['path']
    assert (root/'LICENSE.upstream').is_file()
    assert (root/'NOTICE.upstream').is_file()
    for path in (root/'src').rglob('*.py'):
        if '/native/' not in str(path) and path.name!='benchmark_plugin.py':continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.ImportFrom):
                assert not (node.module or '').startswith('vla_eval'),path
            elif isinstance(node,ast.Import):
                assert not any(a.name.startswith('vla_eval') for a in node.names),path
    assert 'vla-eval' not in (root/'pyproject.toml').read_text().lower()
    requirements = root/'requirements.txt'
    assert requirements.is_file()
    lines = [line.strip() for line in requirements.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith(('#', '--'))]
    assert lines
    for line in lines:
        if not line.startswith('git+'):
            Requirement(line)
    assert plugin.Tasks.dimension_spec
    assert all(path.startswith(('task.', 'params.'))
               for path in plugin.Tasks.dimension_spec.values())

PROFILES=[ROOT/path for entry in CATALOG for path in entry['profiles']]
@pytest.mark.parametrize('path',PROFILES,ids=lambda p: f'{p.parents[2].name}/{p.stem}')
def test_protocol_profiles_generate_valid_configs(path):
    profile=json.loads(path.read_text())
    cfg=extension_application_config(profile,policy_factory='benchmark_fixtures:create_policy')
    loaded=load_application_config(cfg)
    graph_id = 'benchmark_live_rollout' if profile.get('mode') == 'live' else 'benchmark_rollout'
    assert loaded.params.resolve('runtime.graph_id')==graph_id
    assert loaded.task_adapters.task_adapters[0].params['params']==profile.get('params',{})
    assert loaded.graphs.graph_map()[graph_id].outputs['native_metrics']
    assert loaded.graphs.graph_map()[graph_id].outputs['native_dimensions']


def action(session,vector=None):
    return Action(session_id=session.session_id,action_type=ActionType.CUSTOM,
                  payload={'format':'contract_benchmark','native_action':{'actions':vector or [1.0]}})

def test_episode_sequence_horizon_and_observation_consumption():
    async def run():
        env=Environment(params={'done_after':99})
        session=await env.open(SessionSpec(task_id='first',environment_id='contract_benchmark',
                              parameters={'native_episode':3,'native_max_steps':2}))
        first=await session.reset(session.spec)
        assert session.native.task['episode_idx']==1
        assert unpack_observation(first.observation)['first_only'] is True
        await session.observe();await session.observe()
        assert session.native.obs_calls==1
        step1=await session.execute_native(action(session))
        assert not step1.terminal
        step2=await session.execute_native(action(session))
        assert step2.terminal and step2.success is False
        assert step2.info['env_steps']==2
        assert step2.info['benchmark_metrics']['completed_subtasks']==2
        assert session.native.obs_calls==3
        with pytest.raises(RuntimeError):await session.execute_native(action(session))
        await env.close()
        assert session.native.closed
    asyncio.run(run())


def test_success_does_not_consume_an_extra_observation():
    async def run():
        env=Environment(params={'done_after':1})
        session=await env.open(SessionSpec(task_id='first',environment_id='contract_benchmark'))
        await session.reset(session.spec)
        result=await session.execute_native(action(session))
        assert result.success is True
        assert session.native.obs_calls==1
        await env.close()
    asyncio.run(run())


def test_task_case_json_roundtrip_and_seed_is_not_reinvented():
    manifest=task_manifest(ContractBenchmark())
    tasks=Tasks(params={'seed':7},episodes_per_task=3,manifest=manifest,max_tasks=1)
    cases=list(tasks.enumerate_cases(adapter_id='test',params=Params({})))
    assert [c.seed for c in cases]==[7,7,7]
    assert [c.parameters['environment_parameters']['native_episode'] for c in cases]==[0,1,2]
    assert len({c.key for c in cases})==3
    for case in cases:json.dumps(case.to_dict(),allow_nan=False)


def test_codec_roundtrip_preserves_nested_video_dtype_and_literal_keys():
    obs={'images':{'front':np.arange(18,dtype=np.uint8).reshape(2,3,3)},
         'video_history':[np.ones((2,3,3),dtype=np.uint8)],
         'states':np.array([1,2],dtype=np.float32), '$frame':'literal',
         'nested':({'$dict':{'x':True}},[np.zeros((2,2),dtype=np.int16)])}
    packed=pack_observation(obs)
    packed.state=json.loads(json.dumps(packed.state))
    decoded=unpack_observation(packed)
    assert decoded['$frame']=='literal'
    assert isinstance(decoded['nested'],tuple)
    np.testing.assert_array_equal(decoded['images']['front'],obs['images']['front'])
    assert decoded['states'].dtype==np.float32
    assert decoded['nested'][1][0].dtype==np.int16
    obs['images']['front'][:]=0
    assert decoded['images']['front'].sum()>0


@pytest.mark.parametrize('payload',[{}, {'actions':[]},{'actions':[[1,2]]},{'actions':[float('nan')]},{'actions':['x']}])
def test_invalid_action_is_rejected_before_simulator(payload):
    with pytest.raises((ValueError,TypeError)):validate_native_action(payload)


def test_metrics_errors_partial_and_retry_dedup():
    records=[{'case_key':'a','task':'t','attempt':0,'status':'failed','metrics':{'success':True},
              'dimensions':{'suite':'seen','difficulty':'easy'}},
             {'case_key':'a','task':'t','attempt':1,'status':'completed','metrics':{'success':True,'completed_subtasks':5},
              'dimensions':{'suite':'seen','difficulty':'easy'}},
             {'case_key':'b','task':'t','status':'error','metrics':{},
              'dimensions':{'suite':'unseen','difficulty':'hard'}},
             {'case_key':'c','task':'u','status':'pending','metrics':{}}]
    result=aggregate(records,{'success':'mean','completed_subtasks':'mean'},expected_episodes=3)
    assert result['mean_success']==.5
    assert result['mean_completed_subtasks']==5
    assert result['num_errors']==1 and result['partial']
    assert result['overall']['success_rate']==.5
    assert result['overall']['num_successes']==1
    assert result['by_task']==result['tasks']
    assert result['by_dimension']['suite']==[
        {'value':'seen','num_episodes':1,'num_errors':0,'mean_success':1.0,
         'mean_completed_subtasks':5.0,'num_successes':1,'success_rate':1.0},
        {'value':'unseen','num_episodes':1,'num_errors':1,'mean_success':0.0,
         'num_successes':0,'success_rate':0.0},
    ]


def test_extension_dimensions_flow_into_task_case():
    plugin = importlib.import_module('robotenon_simpler.benchmark_plugin')
    manifest = {'tasks':[{'index':0,'name':'pick','fields':{'name':'pick','task_name':'pick'},
                          'rehydrate':False}],
                'metadata':{'max_steps':3},'metric_keys':{'success':'mean'}}
    tasks = plugin.Tasks(manifest=manifest, params={
        'task_name':'pick', 'env_name':'Pick-v0', 'scene_name':'kitchen',
        'env_build_kwargs':{'robot':'google_robot_static'}})
    case = next(tasks.enumerate_cases(adapter_id='simpler', params=Params({})))
    expected = {'task':'pick','environment':'Pick-v0','scene':'kitchen',
                'robot':'google_robot_static'}
    assert case.metadata['dimensions'] == expected
    assert case.parameters['environment_parameters']['native_dimensions'] == expected


def test_episode_file_dimensions_flow_into_summary(tmp_path):
    path = tmp_path/'episode.json'
    path.write_text(json.dumps({
        'key':'simpler:0', 'attempt':0, 'task_id':'pick', 'status':'completed',
        'result':{'outputs':{
            'metric_keys':{'success':'mean'}, 'native_metrics':{'success':True},
            'native_dimensions':{'robot':'google_robot_static', 'scene':'kitchen'},
        }},
    }))
    records, keys = from_episode_files([path])
    summary = aggregate(records, keys, expected_episodes=1)
    assert summary['overall']['success_rate'] == 1.0
    assert summary['by_dimension']['robot'][0]['success_rate'] == 1.0
    assert summary['by_dimension']['scene'][0]['value'] == 'kitchen'


def test_recorder_patches_current_step_without_double_count():
    recorder=EpisodeRecorder()
    recorder.record_step(done=False,success=False)
    recorder.record_step(step=0,done=True,success=True)
    assert recorder._next_step==1
    assert recorder.fields=={'done':True,'success':True}
    recorder.record_step(done=False)
    with pytest.raises(ValueError):recorder.record_step(step=0,done=True)


@pytest.mark.parametrize('cache',['memory','hybrid'])
def test_graph_worker_body_policy_end_to_end(tmp_path,cache):
    cfg=application_config({'benchmark_id':'contract_benchmark','params':{},'max_steps':5},
                           policy_factory='benchmark_fixtures:create_policy')
    for key in ('plugin_environments','plugin_bodies','plugin_task_adapters'):
        cfg[key]=['benchmark_fixtures']
    cfg['safety_state']={'backend':'memory','require_shared':False}
    cfg['observation_cache']={'enabled':True,'backend':cache,
                              'storage':{'root':str(tmp_path/'frames'),'index':str(tmp_path/'frames/index.sqlite3')}}
    config=tmp_path/'config.json';config.write_text(json.dumps(cfg))
    case=list(Tasks(manifest=task_manifest(ContractBenchmark()),max_tasks=1).enumerate_cases(
        adapter_id='contract',params=Params({})))[0]
    result=asyncio.run(run_episode(EpisodeRequest(config=config,output=tmp_path/'result.json'),case=case))
    assert result['graph_status']=='completed',result.get('error')
    assert result['task_success'] is True
    assert result['env_steps']==3
    assert result['outputs']['native_metrics']=={'success':True,'completed_subtasks':3}
    path=tmp_path/'episode.json';path.write_text(json.dumps(result))
    records,keys=from_episode_files([path])
    assert aggregate(records,keys,expected_episodes=1)['mean_success']==1


@pytest.mark.parametrize('backend,workers', [('in_process', 1), ('process', 2)])
def test_benchmark_multi_episode_worker_recycling(tmp_path, backend, workers):
    from robotenon.runtime.experiment import ExperimentRequest, run_experiment
    cfg = application_config({'benchmark_id': 'contract_benchmark', 'params': {},
                              'episodes_per_task': 2, 'max_steps': 5},
                             policy_factory='benchmark_fixtures:create_policy')
    for key in ('plugin_environments', 'plugin_bodies', 'plugin_task_adapters'):
        cfg[key] = ['benchmark_fixtures']
    cfg['task_adapters'][0]['params']['manifest'] = task_manifest(ContractBenchmark())
    cfg['safety_state'] = {'backend': 'memory', 'require_shared': False}
    cfg['parameters']['runtime']['execution'] = dict(
        backend=backend, max_workers=workers, recycle_after_tasks=1,
        startup_timeout_s=15, task_timeout_s=15, shutdown_timeout_s=1, memory_mode='disabled')
    path = tmp_path/'config.json'
    path.write_text(json.dumps(cfg))
    result = asyncio.run(run_experiment(ExperimentRequest(
        config=path, output_dir=tmp_path/'runs', run_id='native-test')))
    assert result['num_errors'] == 0, result
    paths = [p for p in (tmp_path/'runs/native-test').rglob('episode_*.json')
             if '.live' not in p.parts]
    records, keys = from_episode_files(paths)
    assert len(records) == 4
    summary = aggregate(records, keys, expected_episodes=4)
    assert summary['mean_success'] == 1
    assert not summary['partial']


def test_inherited_profile_and_cpu_render_are_preserved():
    simpler = ROOT/'extensions/benchmarks/simpler/configs'
    for index in range(4):
        base = json.loads((simpler/f'widowx_vm-{index}.json').read_text())
        inherited = json.loads((simpler/f'widowx_vm_xvla-{index}.json').read_text())
        assert base['params'] == inherited['params']
    for path in (ROOT/'extensions/benchmarks/robomme/configs').glob('*.json'):
        assert json.loads(path.read_text())['render'] == 'cpu'


def test_isaac_protocol_without_simulator(monkeypatch):
    from types import SimpleNamespace

    from robotenon_behavior1k.benchmark import Behavior1KBenchmark
    from robotenon_robodojo.benchmark import RoboDojoBenchmark
    dojo = RoboDojoBenchmark()
    vector = np.arange(14, dtype=np.float32)
    np.testing.assert_array_equal(dojo._pack_state(dojo._unflatten_action(vector)), vector)
    with pytest.raises(ValueError): dojo._unflatten_action([0]*13)
    env = SimpleNamespace(take_action=lambda a: None, is_episode_end=lambda: True,
                          success=[False], get_obs=dict, get_score=lambda: None,
                          reward_manager=SimpleNamespace(get_score=lambda: [45]))
    dojo._env = env
    dojo._recorder = EpisodeRecorder()
    result = dojo.step({'actions': vector})
    assert result.done and result.info == {'success': False, 'score': .45}
    env.success = [True]
    assert dojo.step({'actions': vector}).info == {'success': True, 'score': 1.0}
    behavior = Behavior1KBenchmark()
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        as_tensor=lambda value, dtype: np.asarray(value, dtype=dtype), float32=np.float32))
    behavior._recorder = EpisodeRecorder()
    behavior._env = SimpleNamespace(step=lambda a, **kw: ({}, 0, False, True, {'done': {'success': False}}))
    behavior._extract_frame = lambda obs: None
    result = behavior.step({'actions': [0]*23})
    assert result.done and result.info['truncated']
    assert behavior.get_step_result(result)['success'] is False
    with pytest.raises(ValueError): behavior.step({'actions': [0]*22})


def test_native_stdout_does_not_corrupt_enumeration(monkeypatch):
    from benchmark_fixtures import NoisyBenchmark
    monkeypatch.setenv('PYTHONPATH', str(ROOT/'tests') + ':' + str(ROOT))
    tasks = Tasks(max_tasks=1)
    tasks.benchmark_class = NoisyBenchmark
    cases = list(tasks.enumerate_cases(adapter_id='noisy', params=Params({})))
    assert len(cases) == 1
    assert cases[0].task_id == 'first'


def test_separate_adapters_do_not_collapse_native_cases(tmp_path):
    paths = []
    for adapter in ('model_a', 'model_b'):
        path = tmp_path/f'{adapter}.json'
        path.write_text(json.dumps({'adapter_id': adapter, 'global_index': 0,
            'task_id': 'same', 'status': 'completed', 'result': {'outputs': {
                'native_case': 'same_benchmark:0:0', 'metric_keys': {'success': 'mean'},
                'native_metrics': {'success': True}}}}))
        paths.append(path)
    records, keys = from_episode_files(paths)
    assert len({r['case_key'] for r in records}) == 2
    assert aggregate(records, keys, expected_episodes=2)['num_episodes'] == 2
