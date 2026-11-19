import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from robotenon.contracts.context import GraphContext
from robotenon.infra.params import Params
from robotenon.modules import ModuleRuntime
from robotenon.modules.plan import PlanModule
from robotenon.modules.verify import VerifyModule
from robotenon.runtime.safety import SafetyStateStore
from robotenon.schemas import EnvironmentFeedback, BodyExecutionResult, BodyExecutionStatus, EnvironmentIO, ActionSpaceSpec
from robotenon.workflow import GraphRuntime
from robotenon.workflow.definition import GraphDefinition
from test_graph_parallel_routing import runtime, node, FunctionModule


def test_mapping_reads_only_declared_owner_and_publishes_only_local_output():
    async def work(ctx):
        if ctx.current_node == 'counter.custom':
            return {'env_steps': 120}
        if ctx.current_node == 'stale':
            return {'env_steps': 0, 'instruction': 'open oven'}
        assert dict(ctx.inputs) == {'steps': 120, 'text': 'open oven'}
        with pytest.raises(TypeError):
            ctx.inputs['steps'] = 0
        return {'accepted': True}
    nodes=[node('counter.custom',next='stale'),node('stale',predecessors=['counter.custom'],next='consumer'),
           node('consumer',predecessors=['stale'],inputs={'steps':'nodes.counter.custom.outputs.env_steps','text':'nodes.stale.outputs.instruction'})]
    result=asyncio.run(runtime(nodes,work,entry='counter.custom').run('main',GraphContext(values={'env_steps':999})))
    assert result.success,result.error
    assert dict(result.context.results['consumer'])=={'accepted':True}


def test_missing_binding_fails_before_module_and_unknown_node_fails_at_compile():
    called=[]
    async def work(ctx):called.append(1)
    result=asyncio.run(runtime([node('start',inputs={'required':'inputs.absent'})],work).run('main'))
    assert not result.success and 'required' in result.error
    assert not called
    with pytest.raises(ValueError,match='unknown node'):
        runtime([node('start',inputs={'x':'nodes.missing.outputs.x'})],work)


def test_mapped_parallel_inputs_use_wave_snapshot():
    async def work(ctx):
        if ctx.current_node=='fast':return {'x': 99}
        await asyncio.sleep(.02)
        assert ctx.inputs['x']==7
        return {'x':ctx.inputs['x']}
    result=asyncio.run(runtime([node('fast'),node('slow',inputs={'x':{'source':'nodes.fast.outputs.x','default':7}})],work,entry=['fast','slow']).run('main'))
    assert result.success,result.error




def test_nested_graph_outputs_resolve_with_identical_local_node_names():
    child=GraphDefinition.model_validate({'id':'child','entry':'same','nodes':[node('same')],
                                        'outputs':{'success':'nodes.same.outputs.ok'}})
    async def work(ctx):return {'ok':True}
    rt=runtime([{'id':'same','graph':'child','type':'graph'}],work,entry='same',graphs=[child])
    rt.graphs['main'].outputs={'success':'nodes.same.outputs.outputs.success'}
    result=asyncio.run(rt.run('main'))
    assert result.success,result.error
    assert result.outputs['success'] is True

@pytest.mark.parametrize('success',[True,False,'true'])
def test_episode_summary_and_live_progress_use_declared_graph_outputs(tmp_path,success):
    from robotenon.runtime.registry import register_module
    from robotenon.modules.base import BaseModule
    from robotenon.runtime.runner import EpisodeRequest,run_episode
    from test_execution_backends import make_config
    name='outcome-test-'+uuid4().hex
    @register_module(name,version='1',kind='evaluator')
    class Outcome(BaseModule):
        async def run(self,ctx):return {'passed':success,'steps_taken':27,'why':'done','instruction':'custom task'}
    path=make_config(tmp_path,trials=1)
    config=json.loads(path.read_text());config['modules']=[{'id':'outcome_module','name':name,'kind':'evaluator'}]
    config['graphs']=[{'id':'test','entry':'totally_custom','nodes':[{'id':'totally_custom','module':'outcome_module','inputs':{}}],
        'outputs':{'success':'nodes.totally_custom.outputs.passed','env_steps':'nodes.totally_custom.outputs.steps_taken',
                   'reason':'nodes.totally_custom.outputs.why','instruction':'nodes.totally_custom.outputs.instruction'}}]
    path.write_text(json.dumps(config));live=tmp_path/'live.json'
    result=asyncio.run(run_episode(EpisodeRequest(config=path,output=tmp_path/'episode.json',live_status_path=live)))
    assert result['graph_success'],result['error']
    assert result['task_success'] is (success is True)
    assert result['env_steps']==27
    assert result['outputs']['instruction']=='custom task'
    assert json.loads(live.read_text())['context']['values']['env_steps']==27
