import asyncio
import json
from uuid import uuid4

import pytest

from robotenon.infra.trace import TraceClient
from robotenon.runtime.runner import EpisodeRequest, run_episode
from robotenon.runtime.observability import SharedObservability
from robotenon.modules.base import BaseModule
from robotenon.runtime.registry import register_module
from test_execution_backends import make_config


def test_summary_counts_preserves_failures_and_bounds_keys():
    async def run():
        spans=[]
        client=TraceClient(lambda batch: spans.extend(batch),mode='summary')
        await client.start()
        for _ in range(10):
            with client.span('node:step', attributes={'kind':'node'}):pass
        with pytest.raises(ValueError):
            with client.span('node:step'):raise ValueError('intentional failure')
        for i in range(300):
            with client.span(f'node:{i}'):pass
        await client.close()
        assert len(spans)==2
        error,summary=spans
        assert error.status=='error' and error.attributes['error']=='intentional failure'
        assert summary.name=='trace:summary'
        stats=summary.attributes['spans']
        assert len(stats)<=257
        assert stats['node:step']['count']==11 and stats['node:step']['errors']==1
        assert sum(v['count'] for v in stats.values())==311
        await client.close() # no duplicate summary on repeated close
        assert len(spans)==2
    asyncio.run(run())


@pytest.mark.parametrize('fail',[False,True])
def test_real_graph_summary_keeps_outcome_and_live_status(tmp_path, monkeypatch, fail):
    import robotenon.workflow.runtime as workflow
    # Summary must not construct detailed input/output trace attributes.
    def forbidden(*args,**kwargs):raise AssertionError('detailed trace serialization invoked')
    monkeypatch.setattr(workflow,'trace_value',forbidden)
    calls=[];name='summary-'+uuid4().hex
    @register_module(name,version='1',kind='evaluator')
    class Counter(BaseModule):
        async def run(self,ctx):
            calls.append(ctx.current_node)
            if fail and len(calls)==2:raise ValueError('graph failure')
            return {'success':True,'steps':len(calls)}
    path=make_config(tmp_path,trials=1);cfg=json.loads(path.read_text())
    cfg['parameters']['runtime']['trace_mode']='summary'
    cfg['modules']=[{'id':'counter','name':name,'kind':'evaluator'}]
    cfg['graphs']=[{'id':'test','entry':'a','nodes':[
        {'id':'a','module':'counter','next':[{'node':'b'}]},
        {'id':'b','module':'counter','predecessors':['a']}],
        'outputs':{'success':'nodes.b.outputs.success','env_steps':'nodes.b.outputs.steps'}}]
    path.write_text(json.dumps(cfg))
    async def run():
        obs=await SharedObservability(tmp_path/'obs').start()
        try:return await run_episode(EpisodeRequest(config=path,output=tmp_path/'result.json',
            observability=obs.source,live_status_path=tmp_path/'live.json'))
        finally:await obs.close()
    result=asyncio.run(run())
    assert result['graph_success'] is (not fail)
    assert calls==['a','b']
    live=json.loads((tmp_path/'live.json').read_text());assert live['status']!='running'
    spans=[json.loads(line) for line in (tmp_path/'obs/trace.jsonl').read_text().splitlines()]
    assert any(x['name']=='trace:summary' for x in spans)
    assert all(x['name']=='trace:summary' or x['status']=='error' for x in spans)
    if fail:assert any(x['status']=='error' and x['name']=='node:test:b' for x in spans)
