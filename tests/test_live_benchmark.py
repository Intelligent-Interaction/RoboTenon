"""Live Graph advances while inference is outstanding; clocks and sessions close."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT/'extensions/benchmarks/benchmark-support'


@pytest.mark.parametrize('wait_first', [False, True])
def test_live_graph_slow_model_and_hold(tmp_path, wait_first):
    package = tmp_path/'robotenon_livefixture'; package.mkdir()
    (package/'__init__.py').write_text('')
    (package/'benchmark_plugin.py').write_text('''
from parity_fixtures import StatefulBenchmark
from robotenon_benchmark_support.integration import register_benchmark
class Fixture(StatefulBenchmark):
    def __init__(self): super().__init__(done_after=6)
    def get_tasks(self): return [{'name':'one'}]
    def get_metadata(self): return {'max_steps':6}
    def get_hold_action(self, last_action): return last_action or {'actions':[0.]}
Environment, Body, Tasks = register_benchmark('livefixture_benchmark', Fixture, module=__name__)
''')
    backend = tmp_path/'vla.py'
    backend.write_text('''
import time
SPEC={'api_version':1,'supported_benchmarks':['livefixture'],'supports_batch':True,
      'history':{'length':2,'interval':1},'replan_steps':1}
class Model:
    def open_session(self, session_id, task_info): assert task_info['mode']=='live'
    def act_batch(self, requests):
        time.sleep(.15)
        return [{'request_id':r['request_id'],'action':{'native_actions':[{'actions':[1.]}]}} for r in requests]
    def close_session(self, session_id, result):
        assert result['status']=='completed', result
        assert 'rt_metrics' in result
    def close(self): pass
def create(): return Model()
''')
    profile = tmp_path/'live.json'
    profile.write_text(json.dumps({'benchmark_id':'livefixture_benchmark','mode':'live','hz':50,
        'pace':1,'wait_first_action':wait_first,'episodes_per_task':2,'params':{}}))
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path),str(ROOT),str(SUPPORT/'src'),str(SUPPORT/'tests')]))
    output=tmp_path/'output'
    result=subprocess.run([sys.executable,'-m','robotenon_benchmark_support.evaluate',
        '--benchmark','livefixture','--profile',str(profile),'--backend',str(backend),
        '--workers-per-gpu','2','--batch-size','2','--wait-ms','5','--output',str(output)],
        env=environment,text=True,capture_output=True,timeout=60)
    assert result.returncode==0, result.stderr+(output/'simulation.log').read_text()
    summary=json.loads((output/'runs/evaluation/summary.json').read_text())
    assert summary['num_episodes']==2
    assert summary['successes']==2
    assert summary['num_errors']==0
    episodes=list((output/'runs/evaluation').glob('*/episode_*/attempts/*/episode.json'))
    assert len(episodes)==2
    for path in episodes:
        out=json.loads(path.read_text())['outputs']
        assert out['env_steps']==6
        rt=out['rt_metrics']
        assert out['elapsed_sec'] == rt['elapsed_sec']
        assert rt['stale_count']>0
        assert 0 < rt['stale_action_ratio'] <= 1
        assert rt['step_time_max']>=rt['step_time_mean']>=0
        if wait_first:
            assert rt['update_count']>=1
        else:
            # Six environment steps complete even if the first .15s model call
            # has not returned. A synchronous loop would take >= .9 seconds.
            assert rt['elapsed_sec'] < .6
    graph=json.loads((output/'config_graphs.json').read_text())['graphs'][0]
    assert graph['id']=='benchmark_live_rollout'
    assert any(n['id']=='live_tick' for n in graph['nodes'])


def test_live_model_error_is_not_swallowed_during_terminal_cleanup():
    import asyncio
    from collections import deque
    from robotenon_benchmark_support.live_runtime import LiveBackendRuntime
    class Broken(LiveBackendRuntime):
        async def infer(self, *args, **kwargs):
            raise RuntimeError('model failed')
    async def run():
        tool=Broken(service='test',live_config={},declaration={'history':{'length':1,'interval':1},'replan_steps':1})
        state={'observations':deque([{}]), 'wake':asyncio.Event(), 'first':asyncio.Event(), 'error':None}
        tool.live['test']=state
        state['wake'].set()
        state['pump']=asyncio.create_task(tool._pump('test', {}))
        await state['pump']
        with pytest.raises(RuntimeError, match='model failed'):
            await tool.end_session('test', {'status':'completed'})
        assert not tool.live
        assert not tool.clients
    asyncio.run(run())
