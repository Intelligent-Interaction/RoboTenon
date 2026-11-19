"""Local candidate audit. Synthetic scheduler results are NOT GPU measurements.
Run with repository, benchmark-support and tests on PYTHONPATH. No production edits.
"""
import argparse
import asyncio
import heapq
import json
import platform
import random
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np
from robotenon.runtime.batch_transport import pack_message, unpack_message


def history_test():
    rng = np.random.default_rng(7)
    frames = [{'images': {str(c): rng.integers(0, 256, (256,256,3), dtype=np.uint8) for c in range(3)},
               'state': rng.random(14).astype(np.float32)} for _ in range(40)]
    results = []
    # Steady-state requests, after initial padding. Same selection as BackendRuntime.
    for stride in (1, 2, 4, 16):
        samples = [[], []]; sizes = [[], []]
        for repeat in range(5):
            caches = [dict(), dict()]
            for step in range(6, 39, stride):
                ids = [step-6, step-4, step-2, step]
                expected = [frames[i] for i in ids]
                for mode in ([0,1] if repeat % 2 else [1,0]):
                    cache = caches[mode]
                    start = time.perf_counter()
                    if mode == 0:
                        wire = pack_message({'history': expected})
                        actual = unpack_message(wire)['history']
                    else:
                        wire = pack_message({'ids':ids, 'new':{str(i):frames[i] for i in ids if i not in cache}})
                        msg = unpack_message(wire)
                        cache.update({int(k):v for k,v in msg['new'].items()})
                        actual = [cache[i] for i in msg['ids']]
                        for i in list(cache):
                            if i < step-6: del cache[i]
                    elapsed = time.perf_counter()-start
                    if step > 6: samples[mode].append(elapsed); sizes[mode].append(len(wire))
                    for a,b in zip(actual, expected):
                        for k in b['images']: np.testing.assert_array_equal(a['images'][k], b['images'][k])
                        np.testing.assert_array_equal(a['state'], b['state'])
        med = [statistics.median(s)*1000 for s in samples]
        results.append({'replan_steps':stride,'history_length':4,'interval':2,
                        'full_ms':med[0], 'delta_ms':med[1], 'speedup':med[0]/med[1],
                        'mean_bytes':[statistics.mean(s) for s in sizes], 'exact':True})
    return results


def simulate(shared, pause, slope, seed):
    """Closed-loop discrete-event model: 128 clients, 8 servers, no real GPU.
    Batch service = 0.4 + slope*(B-1) sec; each client waits for result.
    Each client has its own fixed sequence of simulation delays in both policies.
    All sessions assumed portable; no IPC/migration overhead => optimistic.
    """
    rng=random.Random(seed); workers=128; queries=100
    delays=[[rng.uniform(.5,1.5)*pause for _ in range(queries)] for _ in range(workers)]
    events=[]; serial=0
    def push(t,kind,value):
        nonlocal serial
        serial+=1;heapq.heappush(events,(t,serial,kind,value))
    for w in range(workers):push(delays[w][0],'arrival',(w,0))
    queues=[[] for _ in range(1 if shared else 8)]; busy=[False]*8; batches=[];waits=[];done=0;t=0;completed=set()
    while events:
        t,_,kind,data=heapq.heappop(events)
        if kind=='arrival':
            w,q=data;qi=0 if shared else w%8;queues[qi].append((t,w,q));push(t+.05,'wake',None)
        elif kind=='finish':
            server,batch=data;busy[server]=False
            for _,w,q in batch:
                assert (w,q) not in completed
                completed.add((w,q));done+=1
                if q+1<queries:push(t+delays[w][q+1],'arrival',(w,q+1))
        for server in range(8):
            if busy[server]:continue
            queue=queues[0 if shared else server]
            if not queue or (len(queue)<8 and t < queue[0][0]+.05-1e-9):continue
            batch=queue[:8];del queue[:8];busy[server]=True;batches.append(len(batch));waits.extend(t-a for a,_,_ in batch)
            push(t+.4+slope*(len(batch)-1),'finish',(server,batch))
    assert done==workers*queries and all(not q for q in queues)
    return {'seconds':t,'mean_batch':statistics.mean(batches),'mean_queue_wait':statistics.mean(waits)}


async def graph_test(root):
    from test_execution_backends import make_config
    from robotenon.modules.base import BaseModule
    from robotenon.runtime.registry import register_module
    from robotenon.runtime.runner import EpisodeRequest, run_episode
    from robotenon.runtime.observability import SharedObservability, ObservabilitySource
    from uuid import uuid4
    name='perf-counter-'+uuid4().hex
    calls=[0]
    @register_module(name, version='1', kind='evaluator')
    class Counter(BaseModule):
        async def run(self, ctx):
            calls[0]+=1
            return {'success':True, 'steps':400}
    config_path=make_config(root,trials=1);cfg=json.loads(config_path.read_text())
    cfg['modules']=[{'id':'counter','name':name,'kind':'evaluator'}]
    nodes=[]
    for i in range(400):
        node={'id':f'n{i}','module':'counter'}
        if i:node['predecessors']=[f'n{i-1}']
        if i<399:node['next']=[{'node':f'n{i+1}'}]
        nodes.append(node)
    cfg['graphs']=[{'id':'test','entry':'n0','nodes':nodes,'outputs':{'success':'nodes.n399.outputs.success','env_steps':'nodes.n399.outputs.steps'}}]
    config_path.write_text(json.dumps(cfg));measurements={k:[] for k in ('trace_live','summary_live','no_trace_live','no_trace_no_live')}
    for repeat in range(6):
        order=list(measurements)
        if repeat%2:order.reverse()
        for mode in order:
            directory=root/f'{repeat}-{mode}';directory.mkdir();obs=None
            if mode in ('trace_live','summary_live'):obs=await SharedObservability(directory/'obs').start()
            cfg['parameters']['runtime']['trace_mode']='summary' if mode=='summary_live' else 'full'
            config_path.write_text(json.dumps(cfg))
            start=time.perf_counter(); before=calls[0]
            try:
                result=await run_episode(EpisodeRequest(config=config_path,output=directory/'episode.json',
                    live_status_path=None if mode=='no_trace_no_live' else directory/'live.json',
                    observability=obs.source if obs else ObservabilitySource()))
            finally:
                if obs:await obs.close()
            elapsed=time.perf_counter()-start
            assert result['graph_success'] and result['env_steps']==400 and calls[0]-before==400
            if repeat:measurements[mode].append(elapsed)
    return {k:{'median_ms':statistics.median(v)*1000,'samples_ms':[x*1000 for x in v]} for k,v in measurements.items()}


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    result={'platform':platform.platform(),'python':platform.python_version(),'scope':'local CPU microbenchmarks and explicitly synthetic scheduling sensitivity; no real GPU throughput claim'}
    result['history']=history_test();print('history done',flush=True)
    result['scheduler']=[]
    for pause in (2,6,12):
        for slope in (.02,.1,.4):
            pairs=[(simulate(False,pause,slope,s),simulate(True,pause,slope,s)) for s in (7,8,9)]
            result['scheduler'].append({'sim_pause_sec':pause,'batch_extra_sample_sec':slope,
                'speedup_median':statistics.median(a['seconds']/b['seconds'] for a,b in pairs),
                'fixed':pairs[0][0],'shared':pairs[0][1]})
    print('scheduler done',flush=True)
    with tempfile.TemporaryDirectory(prefix='rc-candidates-',dir='/tmp') as directory:
        result['graph']=asyncio.run(graph_test(Path(directory)))
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))

if __name__=='__main__':main()
