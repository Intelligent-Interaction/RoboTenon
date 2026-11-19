"""Synthetic local controls for bootstrap gate and CPU concurrency, not simulators."""
import argparse
import concurrent.futures
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import statistics
import tempfile
import time


def work(args):
    kind,limit,root=args
    from robotenon.runtime.startup_gate import acquire_startup_gate, release_startup_gate
    acquire_startup_gate(limit,root)
    try:
        if kind=='wait':time.sleep(.05)
        else:
            data=b'x'*65536
            for _ in range(1024):hashlib.sha256(data).digest()
    finally:release_startup_gate()
    return os.getpid()


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();result={'scope':'synthetic CPU/wait controls; not MuJoCo, EGL or model throughput','logical_cpus':os.cpu_count(),'jobs':64,'measurements':[]}
    with tempfile.TemporaryDirectory(prefix='rc-gate-',dir='/tmp') as root:
        with concurrent.futures.ProcessPoolExecutor(max_workers=32,mp_context=multiprocessing.get_context('spawn')) as pool:
            list(pool.map(work,[('wait',0,root)]*64)) # warm process imports before measuring
            for kind in ('wait','cpu'):
                times={x:[] for x in (8,16,32)}
                for repeat in range(3):
                    for limit in ((8,16,32) if repeat%2==0 else (32,16,8)):
                        t=time.perf_counter();ids=list(pool.map(work,[(kind,limit,root)]*64));times[limit].append(time.perf_counter()-t)
                for limit,values in times.items():result['measurements'].append({'kind':kind,'gate_limit':limit,'median_sec':statistics.median(values),'samples':values})
    args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))

if __name__=='__main__':main()
