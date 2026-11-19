"""Local storage/real-IPC comparison; experimental stores never alter runtime defaults.

Run: PYTHONPATH=. python scripts/benchmark_log_storage.py --output report.json
JSONL uses current flush-without-fsync semantics. SQLite WAL FULL and NORMAL
are reported separately; no claim of equal power-loss durability is made.
"""
import argparse
import asyncio
import json
import platform
import random
import sqlite3
import statistics
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

from robotenon.infra.metrics import MetricsDaemon, UnixSocketMetricsSink
from robotenon.infra.trace import TraceDaemon
from robotenon.infra.ingest_server import IngestServer
from robotenon.schemas import MetricSample, TraceSpan


class LegacyMetrics(MetricsDaemon):
    """Pre-optimization per-record persistence; same validation and aggregation."""
    def ingest_batch(self,samples):
        for sample in samples:
            if self.read_only:raise RuntimeError('metrics query source is read-only')
            sample=self._normalize_sample(sample)
            line=sample.model_dump_json()
            with self._lock:
                self._accept_memory(sample)
                if self.wal_path:
                    with self.wal_path.open('a',encoding='utf-8') as handle:
                        handle.write(line+'\n');handle.flush();self._wal_offset=handle.tell()


class Buffer:
    """Intercept only persistence; retain production validation/aggregation/serialization."""
    def __init__(self): self.lines=[]
    def open(self,*a,**kw): return self
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def write(self,line): self.lines.append(line)
    def flush(self): pass
    def tell(self): return 0
    def exists(self): return False


class BatchedMetrics(MetricsDaemon):
    def __init__(self,path,mode):
        super().__init__(None)
        self.path,self.mode=path,mode
        self.wal_path=Buffer()
        self.batch_lock=threading.Lock()
        self.db=None
        if mode.startswith('sqlite'):
            self.db=sqlite3.connect(path,check_same_thread=False)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous='+('FULL' if mode.endswith('full') else 'NORMAL'))
            self.db.execute('CREATE TABLE samples(id INTEGER PRIMARY KEY, name TEXT, episode TEXT, payload TEXT)')
            self.db.execute('CREATE INDEX samples_episode ON samples(episode,id)')
            self.db.commit()
    def ingest_batch(self,samples):
        samples=list(samples)
        with self.batch_lock:
            self.wal_path.lines=[]
            # Reuse production normalization/aggregation without its persistence.
            with self._lock:
                for sample in samples:
                    sample=self._normalize_sample(sample)
                    line=sample.model_dump_json()
                    self._accept_memory(sample)
                    self.wal_path.lines.append(line+'\n')
            lines=self.wal_path.lines
            if self.db:
                self.db.executemany('INSERT INTO samples(name,episode,payload) VALUES(?,?,?)',[
                    (s.name if isinstance(s,MetricSample) else s['name'],
                     str(s.episode_id if isinstance(s,MetricSample) else s['episode_id']),line.rstrip('\n'))
                    for s,line in zip(samples,lines,strict=True)])
                self.db.commit()
            else:
                with self.path.open('a') as handle:
                    handle.writelines(lines)
                    handle.flush()
    def records(self):
        if self.db:return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM samples ORDER BY id')]
        return [json.loads(line) for line in self.path.read_text().splitlines()]
    def close(self):
        if self.db:
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');self.db.close()


def workload(n):
    base=datetime(2026,9,29,tzinfo=timezone.utc)
    metrics=[MetricSample(name=['node_latency_ms','env_steps','policy_calls'][i%3],
        kind=['histogram','counter','counter'][i%3],value=(i%100)+1,run_id='benchmark',
        episode_id=str(i%128),worker_id=str(i%128),gpu_id=str(i%8),
        timestamp=base+timedelta(microseconds=i),labels={'module':['plan','action','verify'][i%3]}) for i in range(n)]
    spans=[TraceSpan(name=['module.plan','module.action','module.verify'][i%3],status='ok',
        run_id='benchmark',episode_id=str(i%128),worker_id=str(i%128),
        start_time=base+timedelta(microseconds=i),end_time=base+timedelta(microseconds=i+1),
        attributes={'node':i%8,'instruction':'Close the fridge door.',
                    'summary':{'count':16,'total_ms':123.4},'payload':'x'*512}) for i in range(n)]
    return metrics,spans


def make_metrics(root,mode):
    if mode=='current_jsonl':return LegacyMetrics(root/'metrics.jsonl')
    if mode=='production_jsonl':return MetricsDaemon(root/'metrics.jsonl')
    return BatchedMetrics(root/('metrics.db' if mode.startswith('sqlite') else 'metrics.jsonl'),mode)


def files_size(root):return sum(p.stat().st_size for p in root.iterdir() if p.is_file())
def p95(values):return sorted(values)[min(len(values)-1,int(len(values)*.95))]


def direct(root,mode,data,batch,trace=False):
    root.mkdir()
    if trace:
        store=TraceDaemon(root/'trace.jsonl') if mode=='dual' else TraceDaemon(db_path=root/'trace.db')
        assert store._db.execute('PRAGMA synchronous').fetchone()[0]==2
    else:store=make_metrics(root,mode)
    lat=[];start=time.perf_counter()
    for i in range(0,len(data),batch):
        t=time.perf_counter();store.ingest_batch(data[i:i+batch]);lat.append((time.perf_counter()-t)*1000)
    elapsed=time.perf_counter()-start
    query=[]
    for _ in range(5):
        t=time.perf_counter()
        if trace:
            found=store._db.execute('SELECT span_id FROM spans WHERE episode_id=? AND run_id=?',('7','benchmark')).fetchall()
        elif isinstance(store,BatchedMetrics) and store.db:
            found=[json.loads(row[0]) for row in store.db.execute('SELECT payload FROM samples WHERE episode=? ORDER BY id',('7',))]
        else:
            path=store.path if isinstance(store,BatchedMetrics) else store.wal_path
            found=[d for line in path.read_text().splitlines() if (d:=json.loads(line)).get('episode_id')=='7']
        query.append((time.perf_counter()-t)*1000)
    assert len(found)==sum(str(d.episode_id)=='7' for d in data)
    if trace:
        assert store._db.execute('SELECT count(*) FROM spans').fetchone()[0]==len(data)
        assert store.snapshot(limit=1)[0].attributes=={**data[-1].attributes,'worker_id':data[-1].worker_id}
        store.close()
    else:
        records=store.records() if isinstance(store,BatchedMetrics) else [json.loads(line) for line in store.wal_path.read_text().splitlines()]
        assert records==[d.model_dump(mode='json') for d in data]
        expected=MetricsDaemon(None);expected.ingest_batch(data)
        assert store._counters==expected._counters and store._histograms==expected._histograms
        if isinstance(store,BatchedMetrics):store.close()
    return {'mode':mode,'records':len(data),'batch':batch,'write_ms':elapsed*1000,
        'records_per_sec':len(data)/elapsed,'batch_p95_ms':p95(lat),
        'episode_query_ms':statistics.median(query),'bytes_after_close':files_size(root),'exact':True}


async def ipc(root,mode,data):
    root.mkdir();store=make_metrics(root,mode)
    # 128 logical workers, bounded to 32 active connections under the existing
    # production server's 64-connection admission limit.
    with tempfile.TemporaryDirectory(prefix='rc-log-',dir='/tmp') as sockets:
        socket=str(Path(sockets)/'s');server=await IngestServer(socket,store).start()
        sink=UnixSocketMetricsSink(socket);gate=asyncio.Semaphore(32);lat=[]
        chunks=[data[i:i+64] for i in range(0,len(data),64)]
        async def worker(index):
            for chunk in chunks[index::128]:
                async with gate:
                    t=time.perf_counter();await sink(chunk);lat.append((time.perf_counter()-t)*1000)
        t=time.perf_counter();await asyncio.gather(*(worker(i) for i in range(128)));elapsed=time.perf_counter()-t
        await server.close()
    records=store.records() if isinstance(store,BatchedMetrics) else [json.loads(line) for line in store.wal_path.read_text().splitlines()]
    assert sorted(records,key=lambda r:datetime.fromisoformat(r['timestamp']))==[d.model_dump(mode='json') for d in data]
    if isinstance(store,BatchedMetrics):store.close()
    return {'mode':mode,'records':len(data),'wall_ms':elapsed*1000,'request_p95_ms':p95(lat),'exact':True}


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--records',type=int,default=16384);p.add_argument('--repeats',type=int,default=3);p.add_argument('--production',action='store_true');args=p.parse_args()
    metrics,spans=workload(args.records)
    result={'platform':platform.platform(),'python':platform.python_version(),'scope':'synthetic production-schema events; production stores and real Unix socket; not simulator throughput',
            'durability':'JSONL flush without fsync; SQLite WAL FULL vs NORMAL; not identical power-loss guarantees','direct':[],'ipc':[]}
    modes=['current_jsonl','production_jsonl'] if args.production else ['current_jsonl','batch_jsonl','sqlite_full','sqlite_normal'];rng=random.Random(7)
    with tempfile.TemporaryDirectory(prefix='robotenon-log-benchmark-') as directory:
        root=Path(directory)
        for repeat in range(args.repeats):
            cases=[(mode,b) for mode in modes for b in [1,64,256]];rng.shuffle(cases)
            for idx,(mode,b) in enumerate(cases):
                x=direct(root/f'm-{repeat}-{idx}',mode,metrics,b);x['repeat']=repeat;x['kind']='metrics';result['direct'].append(x)
            for b in ([] if args.production else [1,64,256]):
                for mode in ['dual','sqlite_only']:
                    x=direct(root/f't-{repeat}-{b}-{mode}',mode,spans,b,True);x['repeat']=repeat;x['kind']='trace';result['direct'].append(x)
            order=modes.copy();rng.shuffle(order)
            for mode in order:
                x=asyncio.run(ipc(root/f'i-{repeat}-{mode}',mode,metrics));x['repeat']=repeat;result['ipc'].append(x)
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n')
            print(f'repeat {repeat+1}/{args.repeats} complete',flush=True)
    print(args.output)

if __name__=='__main__':main()
