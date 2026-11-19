"""Custom framework Graphs, independent services and native benchmark ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / 'extensions/benchmarks/benchmark-support'
SYSTEM = ROOT / 'extensions/benchmarks/systems/robocasa_hierarchical'
REASONER = ROOT / 'extensions/benchmarks/models/fake_reasoner'
for location in (SUPPORT / 'src', SYSTEM / 'module', REASONER / 'src'):
    sys.path.insert(0, str(location))

from robotenon_benchmark_support.system_config import load_system, compose_system, additional_services
from robotenon_benchmark_support.configure import application_config
from robotenon.runtime.config import load_application_config


def test_framework_graph_composition_preserves_benchmark_protocol():
    profile = {'benchmark_id': 'robocasa365_benchmark', 'episodes_per_task': 50,
               'params': {'seed': 7, 'episode_seed_stride': 50, 'split': 'pretrain'}}
    base = application_config(profile)
    config = compose_system(base, load_system(SYSTEM / 'configs/system.json'))
    for key in ('environments', 'bodies', 'task_adapters', 'plugin_environments', 'plugin_bodies', 'plugin_task_adapters'):
        assert config[key] == base[key]
    assert config['parameters']['benchmark_protocol'] == profile
    assert load_application_config(config).params.get('runtime.graph_id', [str, None]) == 'xr365_single_task_reference'
    for graph in config['graphs']:
        ids = {n['id']: n for n in graph['nodes']}
        assert ids['plan']['inputs']['stm']['source'][0] == 'nodes.verify.outputs.stm'
        assert [x['node'] for x in ids['evaluate']['goto']] == ['action', 'plan']
        assert ids['observe']['next'] == [{'node': 'terminal_guard'}]
        assert ids['action']['module'] == 'robocasa_system_action'


def test_system_cannot_override_environment_or_execution(tmp_path):
    raw = json.loads((SYSTEM / 'configs/system.json').read_text())
    raw['graphs_config'] = str(SYSTEM / 'configs/graphs.json')
    for key, value in [('environments', [{'id': 'bad', 'name': 'bad'}]), ('parameters', {'runtime': {'horizon': 1}})]:
        path = tmp_path / 'bad.json'
        path.write_text(json.dumps({**raw, key: value}))
        with pytest.raises(ValueError):
            load_system(path)
    with pytest.raises(ValueError, match='reserved'):
        additional_services([{'name': 'vla', 'factory': 'example:Provider'}])


@pytest.mark.parametrize('batch_size,done_after,contextual', [
    (1, 1, False), (2, 1, False), (1, 7, False), (2, 7, False), (2, 9, False), (2, -1, False),
    (2, 9, True), (2, -1, True),
    (1, 9, 'advisory'), (2, -1, 'advisory'),
])
def test_system_cli_real_graph_and_independent_model_processes(tmp_path, batch_size, done_after, contextual):
    package = tmp_path / 'robotenon_fixture'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'benchmark_plugin.py').write_text('''
from parity_fixtures import StatefulBenchmark
from robotenon_benchmark_support.integration import register_benchmark
class Fixture(StatefulBenchmark):
    def reset(self, task):
        if self.done_after == -1 or getattr(self, 'mixed', False):
            self.mixed = True
            self.done_after = 1 if task['name'] == 'first' else 7
        return super().reset(task)
Environment, Body, Tasks = register_benchmark('fixture_benchmark', Fixture, module=__name__)
''')
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'vla.py').write_text('''
import os
assert os.environ.get('SIMULATION_ONLY') != '1'
SPEC = {'api_version':1,'supported_benchmarks':['fixture'],'supports_batch':True,
        'history':{'length':3,'interval':1},'replan_steps':2}
class VLA:
    def __init__(self): self.sessions = set()
    def open_session(self, session_id, task_info): self.sessions.add(session_id)
    def act_batch(self, requests):
        for r in requests:
            assert r['session_id'] in self.sessions
            obs = r['observation']
            assert obs['task_description'] in ('first','second')
            assert len(obs['history']) == 3
            values = [float(x['state'][0]) for x in obs['history']]
            # Runtime must capture observations during cached-action execution.
            assert all(0 <= b-a <= 1 for a,b in zip(values, values[1:]))
        return [{'request_id':r['request_id'],'action':{'native_actions':[{'actions':[1.]}]*2}} for r in reversed(requests)]
    def close_session(self, session_id, result): self.sessions.remove(session_id)
    def close(self): assert not self.sessions

def create(): return VLA()
''')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'benchmark_id':'fixture_benchmark','params':{'done_after':done_after},
                                  'episodes_per_task':2,'max_steps':7,'render':'gpu'}))
    execution = json.loads((SYSTEM / 'configs/execution.fake.json').read_text())
    execution.update(backend=str(model / 'vla.py'), batch_size=batch_size)
    execution.pop('backend_config')
    for service in execution['batch_services']:
        service['max_batch_size'] = batch_size
    system_path = SYSTEM/'configs/system.json'
    if contextual:
        (package/'reasoner.py').write_text('''
from robotenon_fake_reasoner.backend import Provider as Base
class Provider(Base):
    def infer_batch(self, requests):
        return [{'request_id': r['request_id'], 'output': {'instruction': r['observation']['task']}}
                for r in reversed(requests)]
''')
        execution['batch_services'][0]['factory'] = 'robotenon_fixture.reasoner:Provider'
        mode = 'advisory' if contextual == 'advisory' else 'contextual'
        if mode == 'advisory':
            reasoner_path = package/'reasoner.py'
            reasoner_path.write_text(reasoner_path.read_text().replace(
                "{'instruction': r['observation']['task']}",
                "{'memory': 'Current scene observed.', 'guidance': None}"))
        system = json.loads((SYSTEM/f'configs/system.{mode}.json').read_text())
        system['graphs_config'] = str(SYSTEM/'configs/graphs.json')
        for tool in system['tools']:
            tool['params']['service'] = tool['id']
        system_path = tmp_path/'contextual.json'
        system_path.write_text(json.dumps(system))
    execution_path = tmp_path / 'execution.json'
    execution_path.write_text(json.dumps(execution))
    import shlex
    sim = tmp_path / 'sim-python'
    sim.write_text('#!/bin/sh\nexport SIMULATION_ONLY=1\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    sim.chmod(0o755)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, [tmp_path, ROOT, SUPPORT/'src', SUPPORT/'tests', SYSTEM/'module', REASONER/'src'])))
    out = tmp_path / 'out'
    env['ROBOTENON_LOCAL_RUNTIME_ROOT'] = str(tmp_path / 'runtime')
    result = subprocess.run([sys.executable,'-m','robotenon_benchmark_support.evaluate',
        '--benchmark','fixture','--profile',str(profile),'--system-config',str(system_path),
        '--execution-config',str(execution_path),'--sim-python',str(sim),'--output',str(out)],
        env=env, capture_output=True, text=True, timeout=100)
    assert result.returncode == 0, result.stderr + ((out/'simulation.log').read_text() if (out/'simulation.log').exists() else '')
    summary = json.loads((out/'runs/evaluation/summary.json').read_text())
    assert summary['num_episodes'] == 4
    assert summary['successes'] == (0 if done_after == 9 else 4)
    assert summary['num_errors'] == 0
    episodes = summary['task_adapters']['fixture_benchmark']['episodes']
    for episode in episodes:
        expected_steps = 1 if done_after == 1 or (done_after == -1 and episode['task_id'] == 'first') else 7
        assert episode['env_steps'] == expected_steps
    report = json.loads((out / 'protocol.json').read_text())
    metrics_dir = Path(report['local_runtime']) / 'batch-metrics'
    metrics = [json.loads((metrics_dir / f'{i}-0.json').read_text()) for i in range(3)]
    # The progress planner calls the reasoner on the first chunk and then once
    # every three completed action chunks; VLA and lifecycle request counts are
    # unchanged.
    expected_requests = (
        [4, 4, 0] if done_after == 1
        else [10, 6, 6] if done_after == -1
        else [16, 8, 12]
    )
    if contextual:
        expected_requests[2] = 0
    assert [x['requests'] for x in metrics] == expected_requests
    assert all(x['sessions'] == 0 for x in metrics)
    assert all(set(x['batch_sizes']) <= {str(i) for i in range(1,batch_size+1)} for x in metrics)
    generated=json.loads((out/'config.json').read_text())
    assert set(generated['parameters']['runtime']['execution']['external_batch_services']) == {'vla','planner','verifier'}
    assert report['expected_episodes'] == 4
    assert len(report['model_services']) == 3


def test_vla_receives_subgoal_and_returns_chunk_state_without_extra_inference():
    import asyncio
    from types import SimpleNamespace
    import numpy as np
    from robotenon_benchmark_support.codec import pack_observation
    from robotenon_benchmark_support.runtime import BackendRuntime

    async def check():
        runtime = BackendRuntime(service='vla', declaration={
            'history': {'length': 2, 'interval': 1}, 'replan_steps': 2})
        calls = []
        async def infer(session, payload, metadata):
            calls.append((session, payload, metadata))
            return {'native_actions': [{'actions': [1.]}, {'actions': [2.]}]}
        runtime.infer = infer
        feedback = SimpleNamespace(metadata={'benchmark':'fixture_benchmark','native_case':'stable',
            'native_task':{'name':'whole task'}})
        context = SimpleNamespace(session_id='episode', feedback=feedback, observation=None)
        for index in range(3):
            feedback.observation = pack_observation({'state':np.array([index]),'task_description':'whole task'})
            result = await runtime.call({'instruction':'specific subgoal'}, context)
            assert result.output['needs_plan'] == (index == 1)
        assert len(calls) == 2
        assert all(c[1]['task_description'] == 'specific subgoal' for c in calls)
        assert [int(obs['state'][0]) for obs in calls[-1][1]['history']] == [1,2]
        await runtime.end_session('episode')
        assert not runtime.histories and not runtime.actions
    asyncio.run(check())


def test_qwen_graph_and_custom_parameter_namespaces():
    system = load_system(SYSTEM / 'configs/system.qwen.json')
    system['my_agent'] = {'temperature': 0.25}
    config = compose_system(application_config({'benchmark_id':'robocasa365_benchmark'}), system)
    loaded = load_application_config(config)
    assert loaded.params.get('my_agent.temperature', [float, None]) == 0.25
    assert config['parameters']['model']['hlp_tool'] == 'reasoner'
    assert config['parameters']['model']['verify_tool'] == 'reasoner'
    assert config['tools'][-1]['name'] == 'Qwen3.5-VL'
