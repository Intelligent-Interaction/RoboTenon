"""Local CPU microbenchmark; no simulator, model weights or GPU required.

Run in a checkout with NumPy, msgpack and benchmark-support installed:
python extensions/benchmarks/benchmark-support/tools/benchmark_transport.py --output /tmp/transport.json
"""
import argparse
import asyncio
import tempfile
import json
import platform
import statistics
import subprocess
import time
import types
from pathlib import Path

import numpy as np
from robotenon.runtime import batch_transport as current
from robotenon_benchmark_support.codec import pack_observation, unpack_observation


async def socket_roundtrip(codec, payload, repeats):
    async def echo(reader, writer):
        try:
            for _ in range(repeats + 5):
                value = await codec.read_message(reader)
                await codec.write_message(writer, value)
        finally:
            writer.close()
            await writer.wait_closed()
    with tempfile.TemporaryDirectory(prefix='rc-perf-', dir='/tmp') as directory:
        path = str(Path(directory)/'rpc.sock')
        server = await asyncio.start_unix_server(echo, path=path)
        try:
            reader, writer = await asyncio.open_unix_connection(path)
            samples = []
            for i in range(repeats + 5):
                t = time.perf_counter()
                await codec.write_message(writer, payload)
                result = await codec.read_message(reader)
                if i >= 5: samples.append(time.perf_counter()-t)
            writer.close()
            await writer.wait_closed()
            return statistics.median(samples)
        finally:
            server.close()
            await server.wait_closed()


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--repeats',type=int,default=30);args=p.parse_args()
    root=Path(__file__).resolve().parents[4]
    baseline='014dad454912447ec7bd41055280f05006735ea6'
    old=types.ModuleType('old_transport')
    exec(subprocess.check_output(['git','show',baseline+':robotenon/runtime/batch_transport.py'],cwd=root,text=True),old.__dict__)
    rng=np.random.default_rng(7)
    observations=[{'images':{str(k):rng.integers(0,256,(256,256,3),dtype=np.uint8) for k in range(3)},'state':rng.random(14).astype(np.float32)} for _ in range(4)]
    payload={'history':observations,'task_description':'benchmark transport'}
    old_pack=lambda:json.dumps(old.encode(payload),allow_nan=False,separators=(',',':')).encode()
    old_data=old_pack();new_data=current.pack_message(payload)
    pairs=[('wire_roundtrip',lambda:old.decode(json.loads(old_pack())),lambda:current.unpack_message(current.pack_message(payload)))]
    snapshot=pack_observation(observations[0])
    pairs.append(('observation_decode',lambda:unpack_observation(snapshot),lambda:unpack_observation(snapshot,copy=False)))
    results={}
    for name,before,after in pairs:
        for _ in range(5):before();after()
        samples=[[],[]]
        for i in range(args.repeats):
            for index in ([0,1] if i%2==0 else [1,0]):
                t=time.perf_counter();value=(before,after)[index]();samples[index].append(time.perf_counter()-t);del value
        medians=[statistics.median(s) for s in samples]
        results[name]={'before_median_ms':medians[0]*1000,'after_median_ms':medians[1]*1000,'speedup':medians[0]/medians[1]}
    before=asyncio.run(socket_roundtrip(old,payload,args.repeats))
    after=asyncio.run(socket_roundtrip(current,payload,args.repeats))
    results['unix_socket_echo']={'before_median_ms':before*1000,'after_median_ms':after*1000,'speedup':before/after}
    a=old.decode(json.loads(old_data));b=current.unpack_message(new_data)
    for left,right in zip(a['history'],b['history']):
        for key in left['images']:np.testing.assert_array_equal(left['images'][key],right['images'][key])
        np.testing.assert_array_equal(left['state'],right['state'])
    result={'platform':platform.platform(),'python':platform.python_version(),'numpy':np.__version__,'baseline_commit':baseline,'repeats':args.repeats,'payload':'4 history frames x 3 cameras x 256x256 RGB uint8 + float32 state','old_bytes':len(old_data),'new_bytes':len(new_data),'results':results,'exact_roundtrip':True,'scope':'CPU microbenchmark, not end-to-end simulator or model inference speedup'}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))

if __name__=='__main__':main()
