"""Completion dependencies, coalesced follow-ups, due-time guards and graph stop."""
import asyncio
from uuid import uuid4

import pytest
from test_graph_parallel_routing import node, runtime

from robotenon.contracts.context import GraphContext
from robotenon.storage.context import SQLiteContextStore
from robotenon.workflow.definition import GraphDefinition


@pytest.mark.parametrize('selected', [False, True])
def test_join_requires_both_predecessors_to_select_target(selected):
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append(ctx.current_node)
            return {}
        result = await runtime([
            node('start', next=['a', 'delay']), node('a', predecessors=['start'], next='c'),
            node('delay', predecessors=['start'], next='b'),
            node('b', predecessors=['delay'], next={'node': 'c', 'when': 'true' if selected else 'false'}),
            node('c', predecessors=['a', 'b']),
        ], work).run('main')
        assert result.success, result.error
        assert seen.count('c') == int(selected)
        if selected:
            assert seen.index('c') > seen.index('b')
    asyncio.run(scenario())


def test_backjump_does_not_reuse_previous_round_dependency_completion():
    async def scenario():
        seen = []
        async def work(ctx):
            name = ctx.current_node
            count = ctx.previous.get('count', 0) + 1
            seen.append((name, count))
            return {'count': count}
        result = await runtime([
            node('start', next=['a', {'node': 'b', 'when': "ctx.results['start']['count'] == 1"}]),
            node('a', predecessors=['start'], next='c'), node('b', predecessors=['start'], next='c'),
            node('c', predecessors=['a', 'b'], goto={'node': 'start', 'when': "ctx.results['c']['count'] == 1"}),
        ], work).run('main')
        assert result.success, result.error
        assert ('a', 2) in seen
        assert [item for item in seen if item[0] == 'c'] == [('c', 1)]
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['next', 'goto'])
def test_running_node_retains_exactly_one_followup_with_fresh_snapshot(operation):
    async def scenario():
        started, requests_sent = asyncio.Event(), asyncio.Event()
        calls, active = [], 0
        async def work(ctx):
            nonlocal active
            if ctx.current_node == 'start':
                count = ctx.previous.get('count', 0) + 1
                if count >= 2:
                    await started.wait()
                return {'count': count}
            active += 1
            assert active == 1
            calls.append((ctx.trigger, ctx.results['start']['count']))
            try:
                if len(calls) == 1:
                    started.set()
                    await requests_sent.wait()
                return {'calls': len(calls)}
            finally:
                active -= 1
        runner = runtime([
            node('start', **{operation: {'node': 'target', 'when': "ctx.results['start']['count'] <= 4"}},
                 loop={'when': "ctx.results['start']['count'] < 4"}),
            node('target', predecessors=['start'] if operation == 'next' else []),
        ], work)
        # Release target only after all repeated requests have been routed.
        persist = runner._persist_context_async
        async def persisted(*args, **kwargs):
            value = persist(*args, **kwargs)
            if asyncio.iscoroutine(value):
                await value
            ctx = args[0]
            cursor = next(iter(ctx.cursors.values()), {})
            if ctx.node_records.get('["main"]', {}).get('start', {}).get('values', {}).get('count') == 4 and 'target' in cursor.get('pending', []):
                requests_sent.set()
        runner._persist_context_async = persisted
        result = await asyncio.wait_for(runner.run('main'), 2)
        assert result.success, result.error
        assert calls == [(operation, 1), (operation, 4)]
    asyncio.run(scenario())


def test_due_time_condition_reads_latest_snapshot_and_drops_loop():
    async def scenario():
        calls = []
        async def work(ctx):
            calls.append(ctx.current_node)
            return {'active': False} if ctx.current_node == 'disable' else {}
        runner = runtime([
            node('start', next='disable', loop={'every': '20ms', 'when': "ctx.results['disable'] == null or ctx.results['disable']['active']"}),
            node('disable', predecessors=['start']),
        ], work)
        result = await asyncio.wait_for(runner.run('main'), 1)
        assert result.success, result.error
        assert calls == ['start', 'disable']
    asyncio.run(scenario())


def test_due_guard_preserves_originating_trigger_for_condition():
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append(ctx.trigger)
            return {}
        result = await runtime([
            node('start', loop={'every': '1ms', 'when': "ctx.trigger == 'entry'"}),
        ], work).run('main')
        assert result.success, result.error
        assert seen == ['entry', 'loop']
    asyncio.run(scenario())


@pytest.mark.parametrize('durable', [False, True])
def test_graph_stop_cancels_siblings_and_rejects_swallowed_cancel_publication(tmp_path, durable):
    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def work(ctx):
            if ctx.current_node == 'slow':
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    return {'late': True}
            return {}
        runner = runtime([
            node('start', next=['slow', 'stop'], loop={'every': '60s'}), node('slow', predecessors=['start']),
            {'id': 'stop', 'operation': 'graph.stop', 'predecessors': ['start']},
        ], work)
        if durable:
            runner.context_store = SQLiteContextStore(tmp_path / 'stop.db')
        result = await asyncio.wait_for(runner.run('main', GraphContext(session_id=uuid4())), 2)
        assert result.success, result.error
        assert started.is_set() and cancelled.is_set()
        assert 'slow' not in result.context.results
        cursor = next(iter(result.context.cursors.values()))
        assert cursor['terminal'] and cursor['finished']
        assert not cursor['batches'] and not cursor['pending']
        if durable:
            restored = GraphContext.from_dict(runner.context_store.load(result.context.session_id))
            assert 'slow' not in restored.results
            assert next(iter(restored.cursors.values()))['terminal']
    asyncio.run(scenario())


def test_nested_graph_stop_ends_child_only():
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append(ctx.current_node)
            return {}
        child = GraphDefinition.model_validate({'id': 'child', 'entry': 'stop', 'nodes': [
            {'id': 'stop', 'operation': 'graph.stop'},
        ]})
        result = await runtime([
            {'id': 'start', 'graph': 'child', 'next': 'tail'}, node('tail', predecessors=['start']),
        ], work, graphs=[child]).run('main')
        assert result.success, result.error
        assert seen == ['tail']
    asyncio.run(scenario())


def test_stop_recovery_never_restarts_unfinished_sibling(tmp_path):
    async def scenario():
        calls = []
        async def work(ctx):
            calls.append(ctx.current_node)
            if ctx.current_node == 'slow':
                await asyncio.Event().wait()
            return {}
        runner = runtime([
            node('start', next=['slow', 'stop'], loop={'every': '60s'}), node('slow', predecessors=['start']),
            {'id': 'stop', 'operation': 'graph.stop', 'predecessors': ['start']},
        ], work)
        runner.context_store = SQLiteContextStore(tmp_path / 'resume-stop.db')
        original = runner._supersede_node
        async def crash(*args):
            raise RuntimeError('crash after terminal commit')
        runner._supersede_node = crash
        ctx = GraphContext(session_id=uuid4())
        first = await asyncio.wait_for(runner.run('main', ctx), 2)
        assert not first.success
        restored = GraphContext.from_dict(runner.context_store.load(ctx.session_id))
        assert next(iter(restored.cursors.values()))['terminal']
        runner._supersede_node = original
        second = await asyncio.wait_for(runner.run('main', restored), 2)
        assert second.success, second.error
        assert calls.count('slow') == 1
    asyncio.run(scenario())


def test_waiting_loop_guard_source_survives_checkpoint(tmp_path):
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append(ctx.trigger)
            return {}
        runner = runtime([
            node('start', loop={'every': '1ms', 'when': "ctx.trigger == 'entry'"}),
        ], work)
        runner.context_store = SQLiteContextStore(tmp_path / 'guard.db')
        persist = runner._persist_context_async
        async def crash_after_pending(ctx):
            await persist(ctx)
            cursor = next(iter(ctx.cursors.values()), {})
            if cursor.get('pending_triggers', {}).get('start') == 'loop':
                raise RuntimeError('crash during timer wait')
        runner._persist_context_async = crash_after_pending
        ctx = GraphContext(session_id=uuid4())
        first = await runner.run('main', ctx)
        assert not first.success
        restored = GraphContext.from_dict(runner.context_store.load(ctx.session_id))
        assert next(iter(restored.cursors.values()))['loop_sources']['start'] == 'entry'
        runner._persist_context_async = persist
        second = await runner.run('main', restored)
        assert second.success, second.error
        assert seen == ['entry', 'loop']
    asyncio.run(scenario())


def test_stop_cannot_declare_followup_routes():
    async def work(ctx):
        return {}
    with pytest.raises(ValueError, match='cannot have outgoing routes'):
        runtime([{'id': 'start', 'operation': 'graph.stop', 'loop': True}], work)
