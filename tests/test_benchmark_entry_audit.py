"""Audit all public benchmark commands without booting simulator dependencies."""
import asyncio
import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / 'docs/benchmark-catalog.json').read_text())
sys.path.insert(0, str(ROOT / 'extensions/benchmarks/benchmark-support/src'))
for entry in CATALOG:
    sys.path.insert(0, str(ROOT / entry['path'] / 'src'))

from robotenon_benchmark_support.backend_loader import load
from robotenon_benchmark_support.configure import write_application_config
from robotenon_benchmark_support.evaluate import build_config, parser, profile_path
from robotenon.runtime.config import load_application_config


@pytest.mark.parametrize('entry', CATALOG, ids=lambda e: e['id'])
def test_compatibility_command_preserves_catalog_default(entry, monkeypatch):
    module = importlib.import_module('robotenon_' + entry['id'] + '.evaluate')
    received = []
    monkeypatch.setattr(module, 'evaluate', lambda args: received.extend(args))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--backend', '/external/vla.py', '--output', '/tmp/result'])
    module.main()
    assert received[received.index('--profile') + 1] == Path(entry['default_profile']).stem
    assert received[received.index('--benchmark') + 1] == entry['id']
    received.clear()
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--profile', '/external/profile.json'])
    module.main()
    assert received.count('--profile') == 1
    assert received[received.index('--profile') + 1] == '/external/profile.json'


@pytest.mark.parametrize('entry', CATALOG, ids=lambda e: e['id'])
def test_every_profile_through_unified_config_and_native_action(entry, tmp_path):
    factory, spec = load(str(ROOT / 'extensions/benchmarks/models/fake/vla.py'))
    params = json.loads((ROOT / 'extensions/benchmarks/models/fake/configs' / (entry['id'] + '.json')).read_text())
    from robotenon_benchmark_support.modules import validate_native_action
    model = factory(**params)
    model.open_session('test', {})
    action = model.act_batch([{'session_id':'test', 'request_id':'a'}])[0]['action']['native_actions'][0]
    validate_native_action(action)
    model.close_session('test', {}); model.close()
    for filename in entry['profiles']:
        path = ROOT / filename
        profile = json.loads(path.read_text())
        assert profile_path(entry['id'], path.stem) == path
        args = parser().parse_args(['--benchmark', entry['id'], '--profile', path.stem,
            '--backend', '/external/vla.py', '--output', str(tmp_path), '--model-devices', '0,1',
            '--workers-per-gpu', '2', '--batch-size', '8'])
        config = build_config(profile, spec, ['first', 'second'], args)
        destination = tmp_path / 'config.json'
        write_application_config(config, destination)
        load_application_config(destination)
        actual = config['task_adapters'][0]['params']
        assert actual['params'] == profile.get('params', {})
        for field in ('episodes_per_task', 'max_steps', 'tasks', 'max_tasks'):
            if field in profile:
                assert actual[field] == profile[field]


def test_invalid_live_profile_rejected_before_model_import(tmp_path, monkeypatch):
    import robotenon_benchmark_support.evaluate as evaluator
    def forbidden(*args, **kwargs):
        raise AssertionError('model loading must not precede protocol validation')
    monkeypatch.setattr(evaluator, 'load', forbidden)
    invalid = tmp_path / 'invalid.json'
    invalid.write_text(json.dumps({'benchmark_id':'kinetix_benchmark', 'mode':'live', 'hz':0}))
    args = parser().parse_args(['--benchmark', 'kinetix', '--profile', str(invalid),
        '--backend', '/external/vla.py', '--output', str(tmp_path)])
    with pytest.raises(ValueError, match='hz'):
        asyncio.run(evaluator.run(args))
    assert not (tmp_path / 'model-service.json').exists()
