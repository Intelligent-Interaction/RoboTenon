import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[1]
QWEN=ROOT/'extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen/src'
SYSTEM=ROOT/'extensions/benchmarks/systems/robocasa_hierarchical'
sys.path.insert(0,str(QWEN))
from robotenon_qwen_reasoner.backend import Provider,_has_complete_json_object,messages_for,parse_output


def payload(purpose='planner'):
    return {'purpose':purpose,'prompt':'Return the requested JSON schema','system_prompt':'JSON only',
        'observation':{'images':{'camera':np.arange(48,dtype=np.uint8).reshape(4,4,3)}}}


def test_messages_use_images_and_strict_schemas():
    p=payload();messages=messages_for(p)
    image=messages[1]['content'][1]['image']
    np.testing.assert_array_equal(np.asarray(image),p['observation']['images']['camera'])
    p['observation']['images']['camera'][:]=0
    assert np.asarray(image).sum()>0
    assert parse_output('{"subtask_success":"false"}','verifier') == {"subtask_success":"false"}
    assert parse_output('{"decision":"act","subgoal":null}','planner') == {"decision":"act","subgoal":None}
    with pytest.raises(ValueError):messages_for({**p,'observation':{'images':{}}})


def test_planner_messages_label_previous_and_current_images_without_aliasing():
    p = payload()
    previous = {"camera": np.full((4, 4, 3), 7, dtype=np.uint8)}
    messages = messages_for(p, previous)
    content = messages[1]["content"]
    assert content[0]["text"] == "Previous planner observation — camera: camera"
    assert content[2]["text"] == "Current observation — camera: camera"
    previous["camera"][:] = 0
    p["observation"]["images"]["camera"][:] = 0
    assert np.asarray(content[1]["image"]).sum() > 0
    assert np.asarray(content[3]["image"]).sum() > 0


@pytest.mark.parametrize(('text','complete'), [
    ('', False),
    ('```json\n{"decision":"act"', False),
    ('```json\n{"decision":"act"}\n```', True),
    ('prefix {"outer":{"brace":"}"},"items":[1,2]} trailing analysis', True),
    ('prefix {"outer":{"value":1}', False),
])
def test_json_completion_detects_only_a_complete_top_level_object(text, complete):
    assert _has_complete_json_object(text) is complete


def test_batch_generation_selective_retry_session_and_metrics(tmp_path):
    provider=Provider.__new__(Provider)
    provider.sessions={};provider.counts={'planner':0,'verifier':0,'forward_batches':0,'response_retries':0}
    provider.last_results=[];provider.max_response_retries=1;provider.max_new_tokens=32;provider.metrics_dir=tmp_path
    provider.model=provider.processor=None
    for s in ('a','b'):provider.open_session(s,{})
    calls=[]
    def generate(messages,tokens):
        assert all(len(conversation)==2 for conversation in messages)
        calls.append((len(messages),tokens))
        if len(calls)==1:return [('{"decision":"act","subgoal":"open","completed":[],"remaining":["open"]}',False),('bad',True)]
        return [('{"subtask_success":false,"physical_belief_update":{}}',False)]
    provider._generate=generate
    requests=[{'session_id':'a','request_id':'one','observation':payload()},
              {'session_id':'b','request_id':'two','observation':payload('verifier')}]
    results=provider.infer_batch(requests)
    assert [r['request_id'] for r in results]==['one','two']
    assert calls==[(2,32),(1,64)]
    assert provider.counts=={'planner':1,'verifier':1,'forward_batches':2,'response_retries':1}
    decisions = [json.loads(line) for path in tmp_path.glob('*.decisions.jsonl')
                 for line in path.read_text().splitlines()]
    assert [(row['request_id'], row['session_id']) for row in decisions] == [('one','a'), ('two','b')]
    assert all(row['image_count'] == 1 for row in decisions)
    for s in ('a','b'):provider.close_session(s,{})
    provider.close();m=json.loads(next(tmp_path.glob('*.json')).read_text())
    assert m['status']=='closed' and m['sessions']==0
    assert all(r['image_count']==1 for r in m['last_results'])


def test_system_config_colocates_models_and_routes_by_gpu(tmp_path):
    config=json.loads((SYSTEM/'configs/execution.xiaomi_qwen.json').read_text())
    assert (SYSTEM/'configs'/config['backend']).resolve().is_file()
    assert config['batch_services'][0]['factory']=='robotenon_qwen_reasoner.backend:Provider'
    assert config['model_devices'].split(',')==config['batch_services'][0]['devices']
    assert config['batch_size']==8 and config['wait_ms']==50
    from robotenon.runtime.batch_services import SharedBatchServices
    services=SharedBatchServices([],tmp_path/'metrics')
    services.endpoints={'vla':[('0','v0'),('1','v1')],
                       'reasoner':[('0','q0'),('1','q1')]}
    assert services.for_worker(3,'0')=={'vla':'v0','reasoner':'q0'}
    assert services.for_worker(2,'1')=={'vla':'v1','reasoner':'q1'}


@pytest.mark.parametrize('batch_size',[1,2])
def test_managed_qwen_graph_with_real_provider_logic_and_stub_generation(tmp_path,batch_size):
    # Real processes, image shaping, JSON validation, shared reasoner routing;
    # generation alone is stubbed. This is NOT a real-model smoke test.
    fixture=tmp_path/'robotenon_fixture';fixture.mkdir();(fixture/'__init__.py').write_text('')
    (fixture/'benchmark_plugin.py').write_text('from parity_fixtures import StatefulBenchmark\nfrom robotenon_benchmark_support.integration import register_benchmark\nEnvironment,Body,Tasks=register_benchmark("fixture_benchmark",StatefulBenchmark,module=__name__)\n')
    (tmp_path/'stub_qwen.py').write_text('''
import os,json
assert os.environ.get('SIMULATION_ONLY')!='1'
from pathlib import Path
from robotenon_qwen_reasoner.backend import Provider as RealProvider
class Provider(RealProvider):
    def __init__(self,metrics_dir):
        self.sessions={};self.counts={'planner':0,'verifier':0,'forward_batches':0,'response_retries':0}
        self.last_results=[];self.max_response_retries=1;self.max_new_tokens=64;self.metrics_dir=Path(metrics_dir)
        self.model=self.processor=None
    def _generate(self,conversations,tokens):
        results=[]
        for conversation in conversations:
            content=conversation[1]['content']
            assert any(x['type']=='image' for x in content)
            system=conversation[0]['content'][0]['text'].lower()
            if 'verif' in system: value={'subtask_success':False,'physical_belief_update':{}}
            else: value={'decision':'act','subgoal':'real subgoal','completed':[],'remaining':['task']}
            results.append((json.dumps(value),False))
        return results
''')
    model=tmp_path/'vla.py';model.write_text('''
import os
assert os.environ.get('SIMULATION_ONLY')!='1'
SPEC={'api_version':1,'supported_benchmarks':['fixture'],'supports_batch':True,'history':{'length':3,'interval':1},'replan_steps':2}
class VLA:
    def open_session(self,*args):pass
    def close_session(self,*args):pass
    def close(self):pass
    def act_batch(self,requests):
        assert all(r['observation']['task_description']=='real subgoal' for r in requests)
        return [{'request_id':r['request_id'],'action':{'native_actions':[{'actions':[1.]}]*2}} for r in reversed(requests)]
def create():return VLA()
''')
    profile=tmp_path/'profile.json';profile.write_text(json.dumps({'benchmark_id':'fixture_benchmark','params':{'done_after':7},'episodes_per_task':1,'max_steps':7}))
    config=tmp_path/'execution.json';config.write_text(json.dumps({'backend':str(model),'batch_size':batch_size,'workers_per_gpu':2,
        'batch_services':[{'name':'reasoner','factory':'stub_qwen:Provider','params':{'metrics_dir':str(tmp_path/'qwen-metrics')},'devices':[],'max_batch_size':batch_size,'max_wait_ms':10}]}))
    import shlex
    sim=tmp_path/'sim-python';sim.write_text('#!/bin/sh\nexport SIMULATION_ONLY=1\nexec '+shlex.quote(sys.executable)+' "$@"\n');sim.chmod(0o755)
    support=ROOT/'extensions/benchmarks/benchmark-support'
    env=dict(os.environ,PYTHONPATH=os.pathsep.join(map(str,[tmp_path,ROOT,QWEN,support/'src',support/'tests',SYSTEM/'module'])))
    out=tmp_path/'out'
    result=subprocess.run([sys.executable,'-m','robotenon_benchmark_support.evaluate','--benchmark','fixture','--profile',str(profile),
        '--system-config',str(SYSTEM/'configs/system.qwen_batch.json'),'--execution-config',str(config),'--sim-python',str(sim),'--output',str(out)],env=env,text=True,capture_output=True,timeout=100)
    assert result.returncode==0,result.stderr+((out/'simulation.log').read_text() if (out/'simulation.log').exists() else '')
    summary=json.loads((out/'runs/evaluation/summary.json').read_text())
    assert summary['num_episodes']==2 and summary['num_errors']==0 and summary['successes']==2
    m=json.loads(next((tmp_path/'qwen-metrics').glob('*.json')).read_text())
    assert m['counts']['planner']>0 and m['counts']['verifier']==0
    assert m['status']=='closed' and m['sessions']==0


@pytest.mark.parametrize('architecture,class_name,provider_module', [
    ('qwen3_vl', 'Qwen3VLForConditionalGeneration', 'backend'),
    ('qwen3_5', 'Qwen3_5ForConditionalGeneration', 'backend_qwen35'),
])
def test_reasoner_architecture_loading_is_explicit_and_local(
        tmp_path, monkeypatch, architecture, class_name, provider_module):
    from types import SimpleNamespace
    from importlib import import_module
    cls = import_module('robotenon_qwen_reasoner.' + provider_module).Provider
    (tmp_path / 'config.json').write_text(json.dumps({'model_type': architecture}))
    calls = []
    class Model:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls.append((path, kwargs))
            return cls()
        def to(self, device):
            assert device == 'cuda'
            return self
        def eval(self):
            return self
    class Processor:
        @staticmethod
        def from_pretrained(path, **kwargs):
            assert kwargs == {'local_files_only': True}
            return SimpleNamespace(tokenizer=SimpleNamespace(padding_side='right'))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(bfloat16='bf16'))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        AutoProcessor=Processor, **{class_name: Model}))
    provider = cls(str(tmp_path))
    assert calls == [(str(tmp_path), {
        'dtype': 'bf16', 'attn_implementation': 'sdpa', 'local_files_only': True})]
    assert provider.processor.tokenizer.padding_side == 'left'
    provider.close()
    (tmp_path / 'config.json').write_text(json.dumps({'model_type': 'wrong_model'}))
    with pytest.raises(ValueError, match='model_type='):
        cls(str(tmp_path))
    assert len(calls) == 1  # Wrong checkpoint must fail before weights load.


def test_thinking_completion_ignores_json_in_reasoning():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from robotenon_qwen_reasoner.backend import _answer_text, _JsonCompletionLogitsProcessor
    reasoning = 'Consider a candidate {"instruction":"not final"} and inspect the scene.'
    final = reasoning + '</think>\n{"instruction":"selected instruction"}'
    assert _answer_text(reasoning, True) is None
    assert parse_output(_answer_text(final, True), 'planner') == {'instruction':'selected instruction'}
    tokenizer = SimpleNamespace(batch_decode=lambda *args, **kwargs: [reasoning, final])
    scores = MagicMock()
    processor = _JsonCompletionLogitsProcessor(tokenizer, 1, 42, thinking=True)
    assert processor(np.zeros((2,3), dtype=np.int64), scores) is scores
    scores.__setitem__.assert_called_once_with((1,42),0)
    assert _answer_text('{"instruction":"plain"}') == '{"instruction":"plain"}'
