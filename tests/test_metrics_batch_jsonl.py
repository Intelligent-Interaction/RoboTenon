import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from robotenon.infra.metrics import MetricsDaemon


def sample(value=1, **kwargs):
    return {'name':'events','kind':'counter','value':value,**kwargs}


def test_batch_replay_incremental_reader_and_unicode(tmp_path):
    path=tmp_path/'metrics.jsonl'
    writer=MetricsDaemon(path)
    reader=MetricsDaemon(path,read_only=True)
    writer.ingest_batch([sample(2,labels={'task':'关闭冰箱'}),sample(3,labels={'task':'关闭冰箱'})])
    for source in [writer,reader,MetricsDaemon(path)]:
        assert source.counter_value('events',{'task':'关闭冰箱'})==5
        assert source.counter_value('events',{'task':'关闭冰箱'})==5
    writer.ingest_batch([sample(4,labels={'task':'关闭冰箱'})])
    assert reader.counter_value('events',{'task':'关闭冰箱'})==9
    assert len(path.read_text().splitlines())==3


@pytest.mark.parametrize('failure',['label','histogram','iterator'])
def test_invalid_batch_preserves_only_accepted_prefix(tmp_path,failure):
    path=tmp_path/'metrics.jsonl';writer=MetricsDaemon(path)
    def records():
        yield sample(2)
        if failure=='iterator':raise ValueError('iterator failed')
        if failure=='label':yield sample(3,labels={'bad.name':'x'})
        else:
            yield {'name':'latency','kind':'histogram','value':1,'histogram_buckets':[1,2]}
            yield {'name':'latency','kind':'histogram','value':1,'histogram_buckets':[2,3]}
        yield sample(100)
    with pytest.raises(ValueError):writer.ingest_batch(records())
    assert writer.counter_value('events')==2
    replay=MetricsDaemon(path)
    assert replay.counter_value('events')==2
    assert writer._histograms==replay._histograms
    assert len(path.read_text().splitlines())==(2 if failure=='histogram' else 1)


def test_concurrent_batches_preserve_records_and_aggregates(tmp_path):
    path=tmp_path/'metrics.jsonl';writer=MetricsDaemon(path)
    def write(worker):
        writer.ingest_batch([sample(1,worker_id=str(worker),episode_id=str(i)) for i in range(64)])
    with ThreadPoolExecutor(max_workers=16) as pool:list(pool.map(write,range(32)))
    records=[json.loads(line) for line in path.read_text().splitlines()]
    assert len({(r['worker_id'],r['episode_id']) for r in records})==32*64
    assert len(records)==32*64
    assert MetricsDaemon(path).counter_value('events')==32*64
    assert writer.counter_value('events')==32*64


def test_empty_read_only_and_file_rotation(tmp_path):
    path=tmp_path/'metrics.jsonl';writer=MetricsDaemon(path)
    writer.ingest_batch([])
    assert not path.exists()
    writer.ingest_batch([sample(7)])
    reader=MetricsDaemon(path,read_only=True)
    with pytest.raises(RuntimeError,match='read-only'):reader.ingest_batch([sample(1)])
    replacement=tmp_path/'new.jsonl';MetricsDaemon(replacement).ingest_batch([sample(3)])
    replacement.replace(path)
    assert reader.counter_value('events')==3
