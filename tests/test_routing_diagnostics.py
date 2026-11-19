"""Deterministic trigger merging and actionable AND-join diagnostics."""
import asyncio
from uuid import uuid4

import pytest
from test_graph_parallel_routing import node, runtime

from robotenon.contracts.context import GraphContext
from robotenon.storage.context import SQLiteContextStore


@pytest.mark.parametrize('b_selects,expected_trigger', [(False, 'goto'), (True, 'next')])
def test_goto_merges_with_complete_next_but_bypasses_partial_join(b_selects, expected_trigger):
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append((ctx.current_node, ctx.trigger))
            return {}
        result = await runtime([
            node('start', next=['a', 'delay']),
            node('a', predecessors=['start'], next='c'),
            node('delay', predecessors=['start'], next='b'),
            node('b', predecessors=['delay'], next={'node': 'c', 'when': 'true' if b_selects else 'false'}, goto='c'),
            node('c', predecessors=['a', 'b']),
        ], work).run('main')
        assert result.success, result.error
        assert [(n, t) for n, t in seen if n == 'c'] == [('c', expected_trigger)]
    asyncio.run(scenario())


@pytest.mark.parametrize('b_runs,reason', [(False, 'not_completed'), (True, 'not_selected')])
def test_unreleased_join_reports_missing_predecessor_and_survives_save(tmp_path, b_runs, reason):
    async def scenario():
        async def work(ctx):
            return {}
        runner = runtime([
            node('start', next=['a', {'node': 'b', 'when': 'true' if b_runs else 'false'}]),
            node('a', predecessors=['start'], next='c'),
            node('b', predecessors=['start'], next={'node': 'c', 'when': 'false'}),
            node('c', predecessors=['a', 'b']),
        ], work)
        runner.context_store = SQLiteContextStore(tmp_path / 'diagnostics.db')
        ctx = GraphContext(session_id=uuid4())
        result = await runner.run('main', ctx)
        expected = [{'node': 'c', 'arrived': ['a'], 'missing': {'b': reason}, 'graph': 'main', 'scope': ['main']}]
        assert result.success, result.error
        assert result.routing_diagnostics == expected
        assert 'c' not in result.context.results
        restored = GraphContext.from_dict(runner.context_store.load(ctx.session_id))
        resumed = await runner.run('main', restored)
        assert resumed.routing_diagnostics == expected
        # Returned diagnostics are detached from the durable cursor.
        result.routing_diagnostics[0]['missing'].clear()
        assert result.routing_diagnostics == expected
    asyncio.run(scenario())


def test_satisfied_join_has_no_waiting_diagnostic():
    async def scenario():
        async def work(ctx):
            return {}
        result = await runtime([
            node('start', next=['a', 'b']),
            node('a', predecessors=['start'], next='c'),
            node('b', predecessors=['start'], next='c'), node('c', predecessors=['a', 'b']),
        ], work).run('main')
        assert result.success, result.error
        assert result.routing_diagnostics == []
    asyncio.run(scenario())


def test_next_trigger_loop_condition_repeats_exactly_once():
    async def scenario():
        seen = []
        async def work(ctx):
            if ctx.current_node == 'c':
                seen.append(ctx.trigger)
            return {}
        result = await runtime([
            node('start', next='c'),
            node('c', predecessors=['start'], loop={'every': '1ms', 'when': "ctx.trigger == 'next'"}),
        ], work).run('main')
        assert result.success, result.error
        assert seen == ['next', 'loop']
    asyncio.run(scenario())
