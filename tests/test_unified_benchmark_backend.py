"""Public backend contract, immutable episode identities, and real CLI processes."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / 'extensions/benchmarks/benchmark-support'
sys.path.insert(0, str(SUPPORT / 'src'))
from robotenon_benchmark_support.backend_loader import load, validate_spec
from robotenon_benchmark_support.evaluate import build_config, parser, profile_path
from robotenon_benchmark_support.scheduling import order_cases
from robotenon.task_adapters.evaluator import _ordered_adapter_jobs

SPEC = {'api_version': 1, 'supported_benchmarks': ['fixture'], 'supports_batch': True,
        'history': {'length': 3, 'interval': 2}, 'replan_steps': 2}


def test_external_modules_have_relative_imports_and_isolated_namespaces(tmp_path):
    classes = []
    for i in range(2):
        directory = tmp_path / str(i)
        directory.mkdir()
        (directory / 'helper.py').write_text(f'VALUE = {i}\n')
        (directory / '__init__.py').write_text('from .helper import VALUE\n')
        path = directory / 'vla.py'
        path.write_text(f'from . import VALUE\nSPEC={SPEC!r}\ndef create(): return VALUE\n')
        factory, spec = load(str(path))
        assert factory() == i
        assert spec == SPEC
        classes.append(factory.__module__)
    assert classes[0] != classes[1]


def test_single_only_and_compatibility_checked():
    with pytest.raises(ValueError, match='batch_size=1'):
        validate_spec(dict(SPEC, supports_batch=False), 'fixture', 8)
    with pytest.raises(ValueError, match='does not support'):
        validate_spec(SPEC, 'robocasa365')


def test_shuffle_preserves_cases_and_does_not_consume_global_rng():
    import random
    from robotenon.task_adapters import TaskCase
    cases = [TaskCase(adapter_id='fixture', environment_id='fixture_benchmark', task_id=f'task-{i//3}', index=i,
                     episode=i%3, seed=7+i, metadata={'target50_index': i//3}) for i in range(15)]
    before = random.getstate()
    first = order_cases(cases, {'seed': 7})
    assert random.getstate() == before
    assert first == order_cases(cases, {'seed': 7})
    assert first != cases
    assert {c.key: c.to_dict() for c in first} == {c.key: c.to_dict() for c in cases}


def test_profile_resolution_and_same_graph_for_single_and_batch(tmp_path):
    path = profile_path('robocasa365', 'xiaomi_official')
    assert path.parent.parent == ROOT / 'extensions/benchmarks/robocasa365'
    spec = dict(SPEC, supported_benchmarks=['robocasa365'])
    graphs = []
    for size in (1, 8):
        args = parser().parse_args(['--benchmark','robocasa365','--profile','xiaomi_official',
            '--backend','unused:factory','--output',str(tmp_path),'--model-devices','0,1',
            '--workers-per-gpu','2','--batch-size',str(size)])
        config = build_config(json.loads(path.read_text()), spec, ['a','b'], args)
        execution = config['parameters']['runtime']['execution']
        assert execution['worker_devices'] == ['0','1','0','1']
        assert execution['egl_device_ids'] == ['0','1','0','1']
        assert execution['external_batch_services']['vla'] == ['a','b','a','b']
        assert config['tools'][0]['params']['declaration'] == spec
        assert config['bodies'][0]['params']['heartbeat_ttl_s'] == args.task_timeout + 60
        assert config['parameters']['policy']['action_timeout_ms'] == (args.task_timeout + 60) * 1000
        assert config['parameters']['runtime']['context_history_limit'] == 64
        assert config['parameters']['runtime']['context_outputs_limit'] == 64
        graphs.append(config['graphs'])
    assert graphs[0] == graphs[1]


def test_combined_profiles_build_unique_adapters_and_one_global_plan(tmp_path):
    args = parser().parse_args(['--benchmark', 'fixture', '--profiles', 'one', 'two',
        '--backend', 'unused:factory', '--output', str(tmp_path),
        '--model-devices', '0', '--workers-per-gpu', '2', '--batch-size', '2'])
    profiles = [
        {'benchmark_id': 'fixture_benchmark', 'name': 'first',
         'params': {'suite': 'one'}, 'episodes_per_task': 2},
        {'benchmark_id': 'fixture_benchmark', 'name': 'second',
         'params': {'suite': 'two'}, 'episodes_per_task': 3},
    ]
    config = build_config(profiles, SPEC, ['endpoint'], args)
    assert [item['id'] for item in config['task_adapters']] == ['first', 'second']
    assert [item['params']['params']['suite'] for item in config['task_adapters']] == ['one', 'two']
    assert all(item['params']['scheduling']['scope'] == 'global'
               for item in config['task_adapters'])
    assert {item['params']['episode_plan_path'] for item in config['task_adapters']} == {
        str(tmp_path / 'episode-plan.json')}


def test_global_adapter_queue_is_deterministic_interleaved_and_recorded(tmp_path):
    from robotenon.task_adapters import TaskCase
    schedule = {'scope': 'global', 'strategy': 'global_queue', 'shuffle': True, 'seed': 7}
    configured = []
    for adapter_id in ('first', 'second'):
        config = SimpleNamespace(params={
            'scheduling': schedule,
            'episode_plan_path': str(tmp_path / 'episode-plan.json'),
        })
        owner = SimpleNamespace(config=config)
        cases = [TaskCase(adapter_id=adapter_id, environment_id='fixture_benchmark',
                          task_id=f'{adapter_id}-{index}', index=index)
                 for index in range(5)]
        configured.append((owner, cases))
    first = _ordered_adapter_jobs(configured)
    second = _ordered_adapter_jobs(configured)
    assert [case.key for _, case in first] == [case.key for _, case in second]
    assert [case.adapter_id for _, case in first] != ['first'] * 5 + ['second'] * 5
    plan = json.loads((tmp_path / 'episode-plan.json').read_text())
    assert plan['expected_episodes'] == 10
    assert plan['scheduling']['scope'] == 'global'
    assert [item['global_order'] for item in plan['episodes']] == list(range(10))


@pytest.mark.parametrize('batch_size,done_after,crash,strategy', [(1,3,False,'global_queue'),(2,3,False,'global_queue'),(2,1,False,'global_queue'),(2,3,True,'global_queue'),(2,3,False,'static')])
def test_unified_cli_external_backend_and_subset(tmp_path, batch_size, done_after, crash, strategy):
    # A simulator-only fixture and a model-only module: imports assert process
    # identity, catching any accidental simulator-side backend loading.
    package = tmp_path / 'robotenon_fixture'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'benchmark_plugin.py').write_text(f'''
from parity_fixtures import StatefulBenchmark
from robotenon_benchmark_support.integration import register_benchmark
class Fixture(StatefulBenchmark):
    def __init__(self, done_after=3): super().__init__(done_after=done_after)
    def step(self, action):
        from pathlib import Path
        import os
        marker = Path({str(tmp_path / 'crashed')!r})
        if {crash!r}:
            try: marker.open('x').close()
            except FileExistsError: pass
            else: os._exit(31)
        return super().step(action)
Environment, Body, Tasks = register_benchmark('fixture_benchmark', Fixture, module=__name__)
''')
    backend = tmp_path / 'model'
    backend.mkdir()
    (backend / 'vla.py').write_text(f'''
import os
assert os.environ.get('SIMULATION_ONLY') != '1', 'model imported in simulator'
SPEC = {SPEC!r}
class Model:
    def __init__(self): self.sessions = set()
    def open_session(self, session_id, task_info):
        assert task_info['benchmark_id'] == 'fixture_benchmark'
        assert task_info['episode_id']
        self.sessions.add(session_id)
    def act_batch(self, requests):
        assert all(r['session_id'] in self.sessions for r in requests)
        # Reversed result order explicitly exercises request_id routing.
        return [{{'request_id':r['request_id'], 'action':{{'native_actions':[{{'actions':[1.0]}}]*5}}}} for r in reversed(requests)]
    def close_session(self, session_id, result): self.sessions.remove(session_id)
    def close(self): assert not self.sessions

def create(**params):
    assert params == {{'sentinel': 19}}
    return Model()
''')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'benchmark_id':'fixture_benchmark','params':{'done_after':done_after},'episodes_per_task':3,'max_steps':3,'render':'gpu'}))
    config = tmp_path / 'model.json'; config.write_text('{"sentinel":19}')
    sim = tmp_path / 'sim-python'
    import shlex
    sim.write_text('#!/bin/sh\nexport SIMULATION_ONLY=1\nexec '+shlex.quote(sys.executable)+' "$@"\n')
    sim.chmod(0o755)
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path),str(ROOT),str(SUPPORT/'src'),str(SUPPORT/'tests'),str(ROOT/'tests')]))
    output = tmp_path / 'output'
    environment['ROBOTENON_LOCAL_RUNTIME_ROOT'] = str(tmp_path / 'runtime')
    result = subprocess.run([sys.executable,'-m','robotenon_benchmark_support.evaluate',
        '--benchmark','fixture','--profile',str(profile),'--backend',str(backend/'vla.py'),
        '--backend-config',str(config),'--sim-python',str(sim),'--output',str(output),
        '--workers-per-gpu','2','--batch-size',str(batch_size),'--wait-ms','100', '--scheduling-strategy',strategy],
        env=environment, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr + (output/'simulation.log').read_text()
    summary = json.loads((output/'runs/evaluation/summary.json').read_text())
    assert summary['num_episodes'] == 6
    assert summary['successes'] == 6
    assert summary['num_errors'] == 0
    episode = next((output/'runs/evaluation').glob('*/episode_*.json'))
    saved = json.loads(episode.read_text())['result']['context']
    assert saved['history_limit'] == 64
    assert saved['scoped_outputs_limit'] == 64
    report = json.loads((output/'protocol.json').read_text())
    assert report['expected_episodes'] == 6
    assert report['scheduling']['strategy'] == strategy
    plan = json.loads((output/'episode-plan.json').read_text())
    assert len({c['key'] for c in plan['episodes']}) == 6
    protocol = json.loads((output / 'protocol.json').read_text())
    metrics_dir = Path(protocol['local_runtime']) / 'batch-metrics'
    metrics = json.loads((metrics_dir / '0-0.json').read_text())
    # Three steps per episode, chunks truncated to two: two queries, not three.
    assert metrics['requests'] == (6 if done_after == 1 else 12) + int(crash)
    if batch_size == 2:
        assert metrics['batch_sizes'].get('2', 0) > 0


def test_standalone_xiaomi_backend_and_system_compatibility_share_implementation(monkeypatch):
    standalone, spec = load(str(ROOT / 'extensions/benchmarks/models/xiaomi/vla.py'))
    legacy, legacy_spec = load(str(ROOT / 'extensions/benchmarks/systems/robocasa_hierarchical/tool/xiaomi/vla.py'))
    assert spec == legacy_spec
    assert standalone.__globals__['XiaomiBatchVLA'] is legacy.__globals__['XiaomiBatchVLA']
    monkeypatch.setitem(standalone.__globals__, 'XiaomiBatchVLA', lambda **params: params)
    assert standalone(model_path='fixture') == legacy(model_path='fixture') == {'model_path':'fixture'}
