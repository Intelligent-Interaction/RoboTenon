import asyncio
import json

import numpy as np
import pytest

from robotenon.runtime.history_transport import HistoryCache, MAX_FRAMES
from robotenon.runtime.batch_transport import BatchClient, serve_connection, pack_message, unpack_message, encode
from robotenon.runtime.batching import BatchEngine


def frame(i):
    return {'images':{'rgb':np.full((8,8,3), i, dtype=np.uint8)}, 'state':np.array([i],dtype=np.float32)}


def test_history_wire_reuse_exact_mutation_safe_and_bounded():
    sender, receiver = HistoryCache(), HistoryCache()
    for i in range(100):
        payload={'history':[frame(max(0,i-j)) for j in (2,1,0)],'task_description':'task'}
        msg=sender.prepare(payload)
        restored=receiver.restore(unpack_message(pack_message(msg)))
        sender.acknowledge(msg)
        assert len(msg['frames']) == 1
        assert set(receiver.frames)==set(sender.frames)
        assert len(receiver.frames)<=MAX_FRAMES
        for a,b in zip(restored['history'],payload['history']):
            np.testing.assert_array_equal(a['images']['rgb'],b['images']['rgb'])
            np.testing.assert_array_equal(a['state'],b['state'])
            a['images']['rgb'][:]=255 # models may modify input arrays
        assert restored['task_description']=='task'
    sender.clear();receiver.clear()
    assert not sender.frames and not receiver.frames


def test_history_invalid_messages_are_atomic_and_large_frames_fall_back(monkeypatch):
    import robotenon.runtime.history_transport as module
    sender,receiver=HistoryCache(),HistoryCache()
    msg=sender.prepare({'history':[frame(1)]});receiver.restore(msg)
    before=dict(receiver.frames)
    bad={**msg,'refs':['missing'],'frames':{}}
    with pytest.raises(ValueError,match='miss'):receiver.restore(bad)
    assert dict(receiver.frames)==before
    bad={**msg,'frames':{msg['refs'][0]:b'bad'}}
    with pytest.raises(ValueError,match='identity'):receiver.restore(bad)
    monkeypatch.setattr(module,'MAX_BYTES',10)
    assert sender.prepare({'history':[frame(1)]}) is None
    assert sender.prepare({'other':'data'}) is None
    with pytest.raises(ValueError,match='byte limit'):receiver.restore(msg)


def test_bytes_legacy_and_new_encoding():
    value={'data':b'\x00\xff'}
    assert unpack_message(pack_message(value))==value
    assert unpack_message(json.dumps(encode(value)).encode())==value


@pytest.mark.parametrize('batch_size',[1,2])
def test_history_socket_sessions_reconnect_fallback_and_model_input(batch_size, tmp_path):
    class Model:
        def __init__(self):self.sessions=set()
        def open_session(self,s,metadata):self.sessions.add(s)
        def close_session(self,s,result):self.sessions.remove(s)
        def infer_batch(self,requests):
            result=[]
            for r in requests:
                assert r['session_id'] in self.sessions
                p=r['observation']
                assert set(p)=={'history','task_description'}
                values=[int(f['state'][0]) for f in p['history']]
                for f in p['history']:f['state'][:]=-999
                result.append({'request_id':r['request_id'],'output':{'values':values}})
            return result[::-1]
    async def run():
        # Socket path must fit macOS AF_UNIX limits.
        import tempfile
        with tempfile.TemporaryDirectory(dir='/tmp',prefix='hist-') as directory:
            endpoint=directory+'/sock';model=Model();engine=BatchEngine(model,max_batch_size=batch_size,max_wait_ms=2)
            handlers=set()
            async def handle(r,w):
                task=asyncio.current_task();handlers.add(task)
                try:await serve_connection(engine,r,w)
                finally:handlers.discard(task)
            server=await asyncio.start_unix_server(handle,path=endpoint)
            clients=[BatchClient(endpoint,history_cache=True) for _ in range(2)]
            try:
                for c in clients:await c.open({})
                for i in range(5):
                    outputs=await asyncio.gather(*(c.infer({'history':[frame(j*10+i)],'task_description':str(j)}) for j,c in enumerate(clients)))
                    assert [r['values'] for r in outputs]==[[i],[10+i]]
                # Repeated request must reconstruct clean arrays after model mutation.
                assert (await clients[0].infer({'history':[frame(4)],'task_description':'0'}))['values']==[4]
                # Oversized history uses ordinary infer and resets both cache sides.
                assert len((await clients[0].infer({'history':[frame(1)]*33,'task_description':'0'}))['values'])==33
                assert not clients[0].history_cache.frames
                assert (await clients[0].infer({'history':[frame(4)],'task_description':'0'}))['values']==[4]
                old=clients[0].session_id
                await clients[0].close();assert not clients[0].history_cache.frames
                await clients[0].open({});assert clients[0].session_id!=old
                assert (await clients[0].infer({'history':[frame(4)],'task_description':'0'}))['values']==[4]
            finally:
                for c in clients:await c.close()
                server.close();await server.wait_closed()
                if handlers:await asyncio.gather(*list(handlers))
                await engine.close()
            assert not model.sessions
    asyncio.run(run())


@pytest.mark.parametrize('stride,mode,expected', [(16,'auto',False),(2,'auto',True),(16,'on',True),(2,'off',False)])
def test_unified_configuration_selects_common_cache(tmp_path,stride,mode,expected):
    from robotenon_benchmark_support.evaluate import parser,build_config
    spec={'api_version':1,'supported_benchmarks':['fixture'],'supports_batch':True,
          'history':{'length':4,'interval':2},'replan_steps':stride}
    args=parser().parse_args(['--benchmark','fixture','--profile','smoke','--backend','unused:create',
        '--output',str(tmp_path),'--history-cache',mode])
    config=build_config({'benchmark_id':'fixture_benchmark'},spec,['endpoint'],args)
    assert config['tools'][0]['params']['history_cache'] is expected
    assert config['parameters']['runtime']['trace_mode']=='summary'


def test_history_timeout_discards_connection_cache_and_session():
    from batch_fixtures import EchoProvider
    async def run():
        import tempfile
        with tempfile.TemporaryDirectory(dir='/tmp',prefix='hist-timeout-') as directory:
            model=EchoProvider(delay=.08);engine=BatchEngine(model,max_batch_size=1,max_wait_ms=0);handlers=set()
            async def handle(r,w):
                task=asyncio.current_task();handlers.add(task)
                try:await serve_connection(engine,r,w)
                finally:handlers.discard(task)
            server=await asyncio.start_unix_server(handle,path=directory+'/sock')
            client=BatchClient(directory+'/sock',history_cache=True,timeout_s=.02)
            try:
                await client.open({})
                with pytest.raises(TimeoutError):await client.infer({'history':[frame(1)]})
                assert client.writer is None and not client.history_cache.frames
                if handlers:await asyncio.gather(*list(handlers))
                assert not model.sessions
            finally:
                await client.close();server.close();await server.wait_closed();await engine.close()
    asyncio.run(run())
