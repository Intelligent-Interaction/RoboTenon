"""Logical replicas share physical hardware without sharing routing/session state."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from robotenon.runtime.batch_services import SharedBatchServices
from robotenon.runtime.batch_transport import BatchClient
from robotenon.runtime.devices import validate_gpu_map, physical_device
from robotenon.runtime.execution import ExecutionOptions
from robotenon.runtime.parallel import PersistentExecutionBackend, _worker_thread_environment


@pytest.mark.parametrize('value', [[], {'0': '1'}, {'v0': 'v1'}, {'v0': '0,1'}, {'v0': 0}, {' v0': '0'}])
def test_invalid_mapping(value):
    with pytest.raises(ValueError):
        validate_gpu_map(value)


def test_unknown_alias_and_physical_compatibility():
    with pytest.raises(ValueError, match='unknown virtual'):
        physical_device('typo', {'v0': '0'})
    assert physical_device('2', {'v0': '0'}) == '2'
    assert physical_device('GPU-abc', {}) == 'GPU-abc'


def test_worker_binding_and_physical_resource_deduplication(tmp_path):
    options = ExecutionOptions(backend='process', max_workers=2,
        worker_devices=('v0', 'v1'), virtual_gpu_map={'v0': '0', 'v1': '0'})
    assert options.resource_gpu_ids == ('0',)
    backend = PersistentExecutionBackend(options, SimpleNamespace(output=str(tmp_path/'result.json')), None)
    assert [(s.virtual_gpu_id, s.gpu_id, s.egl_device_id) for s in backend.slots] == [('v0','0','0'), ('v1','0','0')]
    for slot in backend.slots:
        with _worker_thread_environment(1, gpu_id=slot.gpu_id, egl_device_id=slot.egl_device_id, virtual_gpu_id=slot.virtual_gpu_id):
            assert os.environ['CUDA_VISIBLE_DEVICES'] == '0'
            assert os.environ['MUJOCO_EGL_DEVICE_ID'] == '0'
            assert os.environ['ROBOTENON_VIRTUAL_GPU_ID'] == slot.virtual_gpu_id
    assert os.environ.get('ROBOTENON_VIRTUAL_GPU_ID') is None


def test_missing_replica_rejected_before_start(tmp_path):
    services = SharedBatchServices([{'name':'qwen', 'factory':'batch_fixtures:EchoProvider', 'devices':['v0']}], tmp_path,
                                  virtual_gpu_map={'v0':'0','v1':'0'})
    with pytest.raises(ValueError, match='qwen.*v1'):
        services.validate_worker_devices(['v0', 'v1'])
    assert not services.processes


def test_same_physical_gpu_independent_processes_batches_and_sessions(tmp_path):
    async def run():
        services = SharedBatchServices([{'name':'vla', 'factory':'batch_fixtures:EchoProvider',
            'devices':['v0','v1'], 'max_batch_size':2, 'max_wait_ms':1000}], tmp_path,
            virtual_gpu_map={'v0':'0','v1':'0'}, startup_timeout_s=20)
        clients = []
        try:
            await services.start()
            for device in ['v0','v0','v1','v1']:
                client = BatchClient(services.for_worker(0, device)['vla'])
                await client.open({})
                clients.append(client)
            results = await asyncio.gather(*(c.infer(i) for i,c in enumerate(clients)))
            assert [r['value'] for r in results] == [0,1,2,3]
            assert [r['virtual_device'] for r in results] == ['v0','v0','v1','v1']
            assert {r['device'] for r in results} == {'0'}
            assert {r['batch_size'] for r in results} == {2}
            assert results[0]['pid'] == results[1]['pid'] != results[2]['pid'] == results[3]['pid']
            assert {r['count'] for r in results} == {1}
        finally:
            await asyncio.gather(*(c.close() for c in clients))
            await services.close()
        metrics = [json.loads(p.read_text()) for p in tmp_path.glob('*.json')]
        assert {m['virtual_gpu_id'] for m in metrics} == {'v0','v1'}
        assert {m['physical_gpu_id'] for m in metrics} == {'0'}
    asyncio.run(run())


def test_unified_config_preserves_alias_routes_and_physical_egl(tmp_path):
    from robotenon_benchmark_support.evaluate import parser, build_config
    args = parser().parse_args(['--benchmark','fixture','--profile','smoke','--output',str(tmp_path),
        '--model-devices','v0,v1','--workers-per-device','2', '--virtual-gpu-map','{"v0":"0","v1":"0"}'])
    spec = {'api_version':1,'supported_benchmarks':['fixture'],'supports_batch':True,
            'history':{'length':1,'interval':1},'replan_steps':1}
    config = build_config({'benchmark_id':'fixture_benchmark'}, spec, ['socket0','socket1'], args)
    execution = config['parameters']['runtime']['execution']
    assert execution['worker_devices'] == ['v0','v1','v0','v1']
    assert execution['egl_device_ids'] == ['0'] * 4
    assert execution['external_batch_services']['vla'] == ['socket0','socket1','socket0','socket1']


def test_graph_workers_recycle_without_losing_device_binding(tmp_path):
    from test_execution_backends import make_config
    from robotenon.runtime.experiment import ExperimentRequest, run_experiment
    path = make_config(tmp_path, backend='process', workers=2, trials=6, recycle=1)
    config = json.loads(path.read_text())
    config['plugin_modules'] = ['batch_fixtures']
    config['modules'] = [{'name':'virtual_gpu_probe', 'id':'echo'}]
    config['parameters']['runtime']['execution'].update(
        worker_devices=['v0','v1'], virtual_gpu_map={'v0':'0','v1':'0'})
    path.write_text(json.dumps(config))
    result = asyncio.run(run_experiment(ExperimentRequest(config=path, output_dir=tmp_path/'runs', run_id='virtual')))
    assert result['num_errors'] == 0, result
    assert result['num_episodes'] == 6
    statuses = [json.loads(p.read_text()) for p in (tmp_path/'runs').glob('**/attempts/*/status.json')]
    assert len(statuses) == 6
    assert {s['virtual_gpu_id'] for s in statuses} == {'v0','v1'}
    assert {s['physical_gpu_id'] for s in statuses} == {'0'}
    assert len({s['identity']['worker_id'] for s in statuses}) == 6


def test_cli_alias_conflict_and_execution_config(tmp_path, monkeypatch, capsys):
    from robotenon_benchmark_support import evaluate
    backend = tmp_path/'vla.py'
    backend.write_text('def create(): pass\n')
    base = ['--benchmark','fixture','--profile','smoke','--backend',str(backend),'--output',str(tmp_path/'out')]
    with pytest.raises(SystemExit):
        evaluate.main(base + ['--workers-per-device','2','--workers-per-gpu','3'])
    assert 'must agree' in capsys.readouterr().err
    captured = []
    async def run(args):
        captured.append(args)
        return {}
    monkeypatch.setattr(evaluate, 'run', run)
    config = tmp_path/'execution.json'
    config.write_text(json.dumps({'virtual_gpu_map':{'v0':'0','v1':'0'},
        'model_devices':'v0,v1','workers_per_device':16,'batch_size':8,'wait_ms':50}))
    evaluate.main(base + ['--execution-config',str(config)])
    assert captured[0].workers_per_gpu == 16
    assert captured[0].virtual_gpu_map == {'v0':'0','v1':'0'}
