import asyncio,json,sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_execution_backends import make_config
from robosys.runtime.runner import EpisodeRequest,run_episode
from robosys.runtime.execution import ExecutionOptions
from robosys.runtime.parallel import PersistentExecutionBackend
from robosys.storage.safety import SQLiteSafetyStateStore
from robosys.schemas import SafetySignal,SafetyLevel
from robosys.monitors.registry import register_monitor
from robosys.infra.ingest_server import IngestServer
from robosys.bodies.registry import register_body
from robosys.services.body import InProcessBodyService
@register_body("audit-body")
class AuditBody(InProcessBodyService):
    def __init__(self, **kwargs):
        super().__init__(lambda action: None,safety_state=kwargs["safety_state"])

# 审计用例断言当前缺陷存在；通过不代表实现正确，修复后应转换为期望行为回归测试。
def test_configured_egl_mapping_is_discarded():
    options=ExecutionOptions(backend='process',max_workers=2,gpu_ids=('0','1'),egl_device_ids=('3','4'))
    backend=PersistentExecutionBackend(options,EpisodeRequest(),lambda *_:None)
    assert [s.egl_device_id for s in backend.slots]==[None,None]

def test_disabled_monitor_bypasses_shared_safety(tmp_path):
    config=make_config(tmp_path,trials=1)
    data=json.loads(config.read_text())
    path=tmp_path/'safety.sqlite3'
    data['safety_state']={'backend':'sqlite','path':str(path),'require_shared':True}
    config.write_text(json.dumps(data))
    store=SQLiteSafetyStateStore(path)
    store.update([SafetySignal(code='external.estop',level=SafetyLevel.P4,source='audit',latched=True)])
    result=asyncio.run(run_episode(EpisodeRequest(config=config,output=tmp_path/'episode.json')))
    assert store.snapshot().level==SafetyLevel.P4
    assert result['graph_success'] is True
    assert result['context']['safety']['level']=='P0'

MONITOR_EVENTS=[]
@register_monitor('audit-p4-monitor')
class AuditMonitor:
    async def start(self):MONITOR_EVENTS.append('start')
    async def check(self,feedback=None,*,request=None,context=None):
        MONITOR_EVENTS.append('check')
        return {'compliant':False,'safety_level':'P4','reason':'audit','source':'audit'}

def test_configured_monitor_not_used_by_runner(tmp_path):
    MONITOR_EVENTS.clear()
    config=make_config(tmp_path,trials=1)
    data=json.loads(config.read_text())
    data['bodies']=[{'name':'audit-body'}]
    data['monitors']=[{'name':'audit-p4-monitor','id':'audit','modes':['resident','request']}]
    data['parameters']['runtime']['monitor_enabled']=True
    data['graphs'][0]['nodes'].append({'id':'check','type':'system','operation':'monitor.request.audit','predecessors':['echo']})
    config.write_text(json.dumps(data))
    result=asyncio.run(run_episode(EpisodeRequest(config=config,output=tmp_path/'episode.json')))
    assert result['graph_success'] is True
    assert MONITOR_EVENTS==[]

def test_idle_observability_connection_blocks_shutdown(tmp_path):
    class Store:
        def ingest_batch(self,batch):pass
    async def scenario():
        import tempfile
        directory=tempfile.TemporaryDirectory(dir='/tmp',prefix='audit-')
        server=await IngestServer(Path(directory.name)/'test.sock',Store()).start()
        reader,writer=await asyncio.open_unix_connection(str(server.path))
        await asyncio.sleep(.02)
        close=asyncio.create_task(server.close())
        await asyncio.sleep(.15)
        blocked=not close.done()
        writer.close();await writer.wait_closed()
        await asyncio.wait_for(close,1)
        directory.cleanup()
        assert blocked
    asyncio.run(scenario())

from robosys.runtime.experiment import ExperimentRequest,run_experiment
from robosys.tools.registry import register_tool
from robosys.tools.base import BaseTool
TOOL_EVENTS=[]
@register_tool('audit-poison-tool')
class AuditPoisonTool(BaseTool):
    async def call(self,arguments,context):return {}
    async def start(self):TOOL_EVENTS.append('start')
    async def end_session(self,session_id):
        TOOL_EVENTS.append('cleanup_failed')
        raise RuntimeError('session cleanup failed; tool is poisoned')
    async def close(self):TOOL_EVENTS.append('close')

def test_inprocess_reuses_tool_after_cleanup_failure(tmp_path):
    TOOL_EVENTS.clear()
    config=make_config(tmp_path,trials=2)
    data=json.loads(config.read_text())
    data['tools']=[{'name':'audit-poison-tool'}]
    config.write_text(json.dumps(data))
    result=asyncio.run(run_experiment(ExperimentRequest(config=config,output_dir=tmp_path/'out',run_id='test')))
    assert result['num_errors']==2
    assert TOOL_EVENTS==['start','cleanup_failed','cleanup_failed','close']

def test_existing_run_output_is_silently_replaced(tmp_path):
    config=make_config(tmp_path,trials=1)
    request=ExperimentRequest(config=config,output_dir=tmp_path/'out',run_id='same')
    asyncio.run(run_experiment(request))
    path=next((tmp_path/'out/same/test').glob('episode_*.json'))
    original=json.loads(path.read_text())['result']['identity']['attempt_id']
    asyncio.run(run_experiment(request))
    replaced=json.loads(path.read_text())['result']['identity']['attempt_id']
    assert original!=replaced

def test_cross_origin_plain_text_can_invoke_control_endpoint():
    import threading,http.client
    from types import SimpleNamespace
    from robosys.web.server import build_server
    calls=[]
    class Manager:
        def shutdown_all(self,*,reason):
            calls.append(reason)
            return {'stub':True}
    server=build_server('127.0.0.1',0,SimpleNamespace(traces=None),experiment_manager=Manager())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=http.client.HTTPConnection('127.0.0.1',server.server_port)
        client.request('POST','/api/system/shutdown',body='{"reason":"audit"}',headers={'Origin':'https://untrusted.example','Content-Type':'text/plain'})
        response=client.getresponse();response.read()
        assert response.status==200
        assert calls==['audit']
        client.close()
    finally:
        server.shutdown();server.server_close();thread.join()
