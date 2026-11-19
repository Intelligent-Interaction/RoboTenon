"""Production BatchClient/BatchEngine RPC comparison; CPU only, no fake speed claim."""
import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path
import numpy as np
from robotenon.runtime.batch_transport import BatchClient, serve_connection
from robotenon.runtime.batching import BatchEngine

class Provider:
    def infer_batch(self,requests):
        return [{'request_id':r['request_id'],'output':{'steps':[int(f['state'][0]) for f in r['observation']['history']]}} for r in requests]

async def main(output):
    results=[]
    for stride in (2,16):
        observations={}
        def frame(step):
            if step not in observations:
                observations[step]={'images':{str(i):np.full((256,256,3),(step+i)%256,dtype=np.uint8) for i in range(3)},'state':np.array([step],dtype=np.float32)}
            return observations[step]
        payloads=[{'history':[frame(step-i) for i in (6,4,2,0)]} for step in range(6,6+40*stride,stride)]
        samples={False:[],True:[]}
        for repeat in range(4):
            for enabled in ([False,True] if repeat%2 else [True,False]):
                with tempfile.TemporaryDirectory(prefix='hist-perf-',dir='/tmp') as d:
                    engine=BatchEngine(Provider(),max_batch_size=1,max_wait_ms=0);handlers=set()
                    async def handle(r,w):
                        task=asyncio.current_task();handlers.add(task)
                        try:await serve_connection(engine,r,w)
                        finally:handlers.discard(task)
                    server=await asyncio.start_unix_server(handle,path=d+'/sock');client=BatchClient(d+'/sock',history_cache=enabled)
                    try:
                        await client.open({})
                        for index,payload in enumerate(payloads):
                            start=time.perf_counter();value=await client.infer(payload);elapsed=time.perf_counter()-start
                            assert value['steps']==[int(f['state'][0]) for f in payload['history']]
                            if index>=5:samples[enabled].append(elapsed*1000)
                    finally:
                        await client.close();server.close();await server.wait_closed()
                        if handlers:await asyncio.gather(*list(handlers))
                        await engine.close()
        a,b=statistics.median(samples[False]),statistics.median(samples[True])
        results.append({'replan_steps':stride,'history_length':4,'history_interval':2,'samples_per_mode':len(samples[False]),'full_rpc_ms':a,'cached_rpc_ms':b,'speedup':a/b})
    value={'scope':'production Unix socket RPC, synthetic images and model, no GPU inference','results':results}
    output.write_text(json.dumps(value,indent=2)+'\n');print(json.dumps(value,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);asyncio.run(main(p.parse_args().output))
