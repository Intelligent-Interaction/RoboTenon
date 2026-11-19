import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from robotenon.modules.base import BaseModule
from robotenon.runtime.registry import register_module
from robotenon.runtime.runner import EpisodeRequest, run_episode
from test_execution_backends import make_config


@pytest.mark.parametrize('fail', [False, True])
def test_live_window_throttles_without_changing_graph_and_flushes_terminal(tmp_path, monkeypatch, fail):
    import robotenon.runtime.runner as runner
    import robotenon.task_adapters.evaluator as evaluator

    clock = [0.0]
    monkeypatch.setattr(runner, 'time', SimpleNamespace(**{**vars(runner.time), 'monotonic': lambda: clock[0]}))
    live = tmp_path / 'live.json'
    snapshots = []
    original = evaluator.write_json

    def capture(path, value):
        if path == live:
            snapshots.append(json.loads(json.dumps(value)))
        original(path, value)

    monkeypatch.setattr(evaluator, 'write_json', capture)
    calls = []
    name = 'live-window-' + uuid4().hex

    @register_module(name, version='1', kind='evaluator')
    class Counter(BaseModule):
        async def run(self, ctx):
            calls.append(ctx.current_node)
            clock[0] += .01
            if fail and len(calls) == 300:
                raise RuntimeError('intentional-final-node-failure')
            return {'success': True, 'steps': len(calls)}

    config_path = make_config(tmp_path, trials=1)
    config = json.loads(config_path.read_text())
    config['modules'] = [{'id': 'counter', 'name': name, 'kind': 'evaluator'}]
    nodes = []
    for i in range(300):
        node = {'id': f'n{i}', 'module': 'counter'}
        if i: node['predecessors'] = [f'n{i-1}']
        if i < 299: node['next'] = [{'node': f'n{i+1}'}]
        nodes.append(node)
    config['graphs'] = [{'id': 'test', 'entry': 'n0', 'nodes': nodes,
                         'outputs': {'success': 'nodes.n299.outputs.success',
                                     'env_steps': 'nodes.n299.outputs.steps'}}]
    config_path.write_text(json.dumps(config))
    result = asyncio.run(run_episode(EpisodeRequest(config=config_path, output=tmp_path/'episode.json', live_status_path=live)))
    assert len(calls) == 300
    assert result['graph_success'] is (not fail)
    if not fail:
        assert result['env_steps'] == 300
    final = json.loads(live.read_text())
    assert final['status'] != 'running'
    assert len(final['timeline_spans']) == 256
    assert final['timeline_window'] == {'limit': 256, 'total_spans': 300, 'truncated': True}
    assert final['timeline_spans'][-1]['attributes']['node'] == 'n299'
    assert len({s['span_id'] for s in final['timeline_spans']}) == 256
    assert len(final['context']['history']) <= 256
    assert len(snapshots) <= 7  # ~600 events, ~3 simulated seconds, forced final/error flushes
    assert all(len(s['timeline_spans']) <= 256 for s in snapshots)
    if fail:
        assert any(s['timeline_spans'] and s['timeline_spans'][-1]['status'] == 'error' for s in snapshots)
