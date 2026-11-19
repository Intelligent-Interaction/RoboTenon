"""Static condition references and bounded, versioned routing observations."""
import asyncio

import pytest
from test_graph_parallel_routing import node, runtime

from robotenon.contracts.context import GraphContext
from robotenon.infra.metrics import MetricsClient
from robotenon.infra.trace import TraceClient
from robotenon.workflow.conditions import condition_node_references


@pytest.mark.parametrize('expression', [
    "ctx.results['typo']['ready']", 'ctx.results.typo.ready',
    "ctx.result_versions['typo'] > 0",
])
@pytest.mark.parametrize('operation', ['next', 'goto', 'loop'])
def test_unknown_static_condition_node_fails_at_assembly(expression, operation):
    async def work(ctx):
        return {}
    rule = {'when': expression}
    if operation != 'loop':
        rule['node'] = 'END'
    with pytest.raises(ValueError, match='unknown result nodes.*typo'):
        runtime([node('start', **{operation: rule})], work)


def test_reference_check_does_not_treat_dynamic_fields_or_nested_keys_as_nodes():
    assert condition_node_references("ctx.results['start']['unknown_field'] == null") == {'start'}
    assert condition_node_references("ctx.results[ctx.inputs['chosen']]['field']") == set()
    async def scenario():
        async def work(ctx):
            return {}
        runner = runtime([node('start', next={'node': 'END', 'when': "ctx.results['start']['unknown_field'] == null"})], work)
        assert (await runner.run('main')).success
    asyncio.run(scenario())


def test_condition_trace_identifies_phase_trigger_and_read_versions():
    async def scenario():
        async def work(ctx):
            return {}
        runner = runtime([
            node('start', next='c'),
            node('c', predecessors=['start'], loop={'every': '1ms', 'when': "ctx.trigger == 'next'"}),
        ], work)
        runner.trace = TraceClient()
        result = await runner.run('main')
        assert result.success, result.error
        events = [e for e in result.routing_events if e.get('operation') == 'loop']
        assert [(e['phase'], e['trigger'], e['matched']) for e in events] == [
            ('after_completion', 'next', True), ('before_loop', 'next', True),
            ('after_completion', 'loop', False),
        ]
        assert events[0]['snapshot_versions'] == events[1]['snapshot_versions']
        assert events[2]['snapshot_versions']['c'] > events[0]['snapshot_versions']['c']
        assert any(span.attributes.get('phase') == 'before_loop' for span in runner.trace.buffer)
        assert any(span.attributes.get('trigger') == 'loop' and span.attributes.get('kind') == 'node'
                   for span in runner.trace.buffer)
    asyncio.run(scenario())


def test_periodic_metrics_measure_overrun_and_export_to_existing_client():
    async def scenario():
        async def work(ctx):
            count = ctx.previous.get('count', 0) + 1
            if count == 1:
                await asyncio.sleep(.035)
            return {'count': count}
        runner = runtime([node('start', loop={'every': '10ms', 'when': "ctx.results['start']['count'] < 3"})], work)
        runner.metrics = MetricsClient()
        result = await asyncio.wait_for(runner.run('main'), 2)
        assert result.success, result.error
        stats = result.loop_metrics[0]
        assert stats['target_interval_ms'] == 10
        assert stats['invocations'] == 3 and stats['loop_invocations'] == 2
        assert stats['skipped_ticks'] >= 2
        assert stats['duration_total_ms'] >= 35
        assert stats['interval_samples'] == 2
        names = {sample.name for sample in runner.metrics.buffer}
        assert names >= {'workflow.loop.duration_ms', 'workflow.loop.interval_ms',
                         'workflow.loop.skipped_ticks', 'workflow.loop.target_interval_ms'}
    asyncio.run(scenario())


def test_routing_observations_are_bounded_and_survive_serialization():
    async def scenario():
        async def work(ctx):
            return {'count': ctx.previous.get('count', 0) + 1}
        result = await runtime([node('start', loop={'when': "ctx.results['start']['count'] < 90"})], work).run('main')
        assert result.success, result.error
        assert len(result.routing_events) == 200
        assert result.routing_events_dropped > 0
        restored = GraphContext.from_dict(result.context.to_dict())
        cursor = next(iter(restored.cursors.values()))
        assert cursor['routing_events'] == result.routing_events
        assert cursor['loop_metrics']['start']['invocations'] == 90
        copied = result.routing_events
        copied[0]['node'] = 'mutated'
        assert result.routing_events[0]['node'] == 'start'
    asyncio.run(scenario())


def test_node_trace_reads_current_workspace_output():
    async def scenario():
        async def work(ctx):
            return {'plan': {'subgoal': 'close the door'}, 'input_text': ctx.inputs['task']}
        runner = runtime([node('plan')], work, entry='plan')
        runner.trace = TraceClient()
        result = await runner.run('main', initial_values={'task':'test instruction'})
        assert result.success, result.error
        spans = [s for s in runner.trace.buffer if s.name == 'node:main:plan']
        assert spans[0].attributes['output']['plan']['subgoal'] == 'close the door'
        assert spans[0].attributes['input']['task'] == 'test instruction'
    asyncio.run(scenario())
