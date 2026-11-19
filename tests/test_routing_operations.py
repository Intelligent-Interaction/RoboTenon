"""Independent routing syntax and self-loop/predecessor publication races."""
import asyncio
from uuid import uuid4

import pytest
from test_graph_parallel_routing import node, runtime

from robotenon.contracts.context import GraphContext
from robotenon.storage.context import SQLiteContextStore
from robotenon.workflow.definition import GraphNode


@pytest.mark.parametrize('field,value', [
    ('next', {'node': 'b', 'kind': 'loop'}),
    ('goto', {'node': 'b', 'kind': 'loop'}),
    ('next', 'goto b'),
    ('goto', 'next b'),
    ('loop', {'node': 'other'}),
    ('loop', {'node': 'a'}),
    ('loop', 'other'),
    ('loop', [{'when': 'true'}]),
])
def test_operations_reject_mixed_syntax(field, value):
    with pytest.raises(ValueError):
        GraphNode.model_validate(node('a', **{field: value}))


def test_three_operations_have_no_kind_discriminator():
    parsed = GraphNode.model_validate(node('a', next='next b when true', goto='goto a when false', loop={'when': 'true'}))
    assert parsed.next[0].node == 'b'
    assert parsed.goto[0].node == 'a'
    assert parsed.loop.when == 'true'
    assert all('kind' not in rule.model_dump() for _, rule in parsed.routes())


@pytest.mark.parametrize('loop', [True, {'when': "ctx.results['start']['count'] < 3"}])
def test_self_loop_is_conditional_and_bounded(loop):
    async def scenario():
        async def work(ctx):
            return {'count': ctx.previous.get('count', 0) + 1}
        runner = runtime([node('start', loop=loop)], work)
        runner.compiled['main'].definition.max_steps = 3
        result = await runner.run('main')
        assert result.status == ('max_steps' if loop is True else 'completed')
        assert result.context.results['start']['count'] == 3
    asyncio.run(scenario())


def test_self_loop_runs_repeatedly_while_slow_branch_is_pending():
    async def scenario():
        enough_ticks = asyncio.Event()
        async def work(ctx):
            if ctx.current_node == 'slow':
                await asyncio.wait_for(enough_ticks.wait(), 1)
                return {'ready': True}
            count = ctx.previous.get('count', 0) + 1
            if count == 4:
                enough_ticks.set()
            return {'count': count}
        runner = runtime([
            node('start', next='slow', loop={'when': "ctx.results['start']['count'] < 4"}),
            node('slow', predecessors=['start']),
        ], work)
        result = await asyncio.wait_for(runner.run('main'), 2)
        assert result.success, result.error
        assert result.context.results['start']['count'] == 4
    asyncio.run(scenario())


@pytest.mark.parametrize('swallow_cancel', [False, True])
@pytest.mark.parametrize('durable', [False, True])
def test_predecessor_replaces_loop_without_stale_ctx_publication(tmp_path, swallow_cancel, durable):
    async def scenario():
        loop_started = asyncio.Event()
        calls = {'a': 0, 'b': 0, 'c': 0}
        active_c = 0
        drafts = []
        observed = []

        async def work(ctx):
            nonlocal active_c
            name = ctx.current_node
            calls[name] += 1
            if name == 'a':
                if calls[name] == 2:
                    await asyncio.wait_for(loop_started.wait(), 1)
                return {'round': calls[name]}
            if name == 'b':
                return {'round': ctx.results['a']['round']}
            active_c += 1
            assert active_c == 1
            try:
                round_number = ctx.results['b']['round']
                observed.append(round_number)
                if calls[name] == 2:
                    ctx.values['stale'] = True
                    ctx.task_id = 'stale'
                    drafts.append(ctx.values)
                    loop_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        # This invocation must retain its original immutable view.
                        assert ctx.results['b']['round'] == 1
                        if not swallow_cancel:
                            raise
                    return {'round': -1, 'stale': True}
                assert 'stale' not in ctx.previous
                assert ctx.task_id == 'original'
                return {'round': round_number}
            finally:
                active_c -= 1

        runner = runtime([
            node('a', next='b'),
            node('b', predecessors=['a'], next='c'),
            node('c', predecessors=['b'], goto=[{'node': 'a', 'when': "ctx.results['c']['round'] == 1"}],
                 loop={'when': "ctx.results['c']['round'] == 1"}),
        ], work, entry='a')
        if durable:
            runner.context_store = SQLiteContextStore(tmp_path / 'race.db')
        ctx = GraphContext(session_id=uuid4(), task_id='original')
        result = await asyncio.wait_for(runner.run('main', ctx), 3)
        assert result.success, result.error
        assert observed == [1, 1, 2]
        assert calls == {'a': 2, 'b': 2, 'c': 3}
        assert ctx.results['c'] == {'round': 2}
        assert ctx.task_id == 'original'
        with pytest.raises(RuntimeError, match='closed'):
            drafts[0]['late'] = True
        assert not ctx._pending_publications
        if durable:
            restored = GraphContext.from_dict(runner.context_store.load(ctx.session_id))
            assert restored.results['c'] == {'round': 2}
            assert restored.task_id == 'original'
    asyncio.run(scenario())


def test_all_predecessors_must_arrive_before_loop_is_invalidated():
    async def scenario():
        loop_started, one_arrived, release_other = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = {'a': 0, 'b': 0, 'c': 0, 'd': 0}
        cancelled = asyncio.Event()
        async def work(ctx):
            name = ctx.current_node
            seen[name] += 1
            if name == 'a' and seen[name] == 2:
                await loop_started.wait()
            if name == 'b' and seen[name] == 2:
                one_arrived.set()
            if name == 'd' and seen[name] == 2:
                await release_other.wait()
            if name == 'c' and seen[name] == 2:
                loop_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return {'count': ctx.previous.get('count', 0) + 1}
        runner = runtime([
            node('a', next=['b', 'd']),
            node('b', predecessors=['a'], next='c'),
            node('d', predecessors=['a'], next='c'),
            node('c', predecessors=['b', 'd'], goto=[{'node': 'a', 'when': "ctx.results['c']['count'] < 2"}],
                 loop={'when': "ctx.results['c']['count'] < 2"}),
        ], work, entry='a')
        task = asyncio.create_task(runner.run('main'))
        await asyncio.wait_for(one_arrived.wait(), 1)
        await asyncio.sleep(0.02)
        assert not cancelled.is_set()
        release_other.set()
        result = await asyncio.wait_for(task, 2)
        assert result.success, result.error
        assert cancelled.is_set()
    asyncio.run(scenario())


def test_recovery_preserves_loop_and_slow_branch_read_snapshots(tmp_path):
    async def scenario():
        loop_started, slow_done = asyncio.Event(), asyncio.Event()
        calls = {'start': 0, 'slow': 0}
        resume = False
        async def work(ctx):
            name = ctx.current_node
            calls[name] += 1
            if name == 'slow':
                await loop_started.wait()
                slow_done.set()
                return {'ok': True}
            count = ctx.previous.get('count', 0) + 1
            if count == 2:
                assert 'slow' not in ctx.results
                loop_started.set()
                await slow_done.wait()
                if not resume:
                    raise RuntimeError('interrupted self loop')
            return {'count': count}
        runner = runtime([
            node('start', next=[{'node': 'slow', 'when': "ctx.results['start']['count'] == 1"}],
                 loop={'when': "ctx.results['start']['count'] < 2"}),
            node('slow', predecessors=['start']),
        ], work)
        runner.context_store = SQLiteContextStore(tmp_path / 'resume.db')
        ctx = GraphContext(session_id=uuid4())
        first = await asyncio.wait_for(runner.run('main', ctx), 2)
        assert not first.success
        restored = GraphContext.from_dict(runner.context_store.load(ctx.session_id))
        resume = True
        second = await asyncio.wait_for(runner.run('main', restored), 2)
        assert second.success, second.error
        assert calls == {'start': 3, 'slow': 1}
    asyncio.run(scenario())


def test_resume_after_durable_loop_invalidation_does_not_restart_old_loop(tmp_path):
    async def scenario():
        started = asyncio.Event()
        calls = {'a': 0, 'c': 0}
        async def work(ctx):
            name = ctx.current_node
            calls[name] += 1
            if name == 'a':
                if calls[name] == 2:
                    await started.wait()
                return {'round': calls[name]}
            if calls[name] == 2:
                started.set()
                await asyncio.Event().wait()
            return {'round': ctx.results['a']['round']}
        runner = runtime([
            node('a', next='c'),
            node('c', predecessors=['a'], goto=[{'node': 'a', 'when': "ctx.results['c']['round'] == 1"}],
                 loop={'when': "ctx.results['c']['round'] == 1"}),
        ], work, entry='a')
        runner.context_store = SQLiteContextStore(tmp_path / 'invalidated.db')
        supersede = runner._supersede_node
        async def crash(*args):
            await supersede(*args)
            raise RuntimeError('crash after durable invalidation')
        runner._supersede_node = crash
        ctx = GraphContext(session_id=uuid4())
        first = await asyncio.wait_for(runner.run('main', ctx), 2)
        assert not first.success
        restored = GraphContext.from_dict(runner.context_store.load(ctx.session_id))
        cursor = next(iter(restored.cursors.values()))
        assert any(batch['superseded'] == ['c'] for batch in cursor['batches'].values())
        runner._supersede_node = supersede
        second = await asyncio.wait_for(runner.run('main', restored), 2)
        assert second.success, second.error
        assert calls == {'a': 2, 'c': 3}
        assert restored.results['c']['round'] == 2
    asyncio.run(scenario())


def test_simultaneous_predecessor_completion_suppresses_old_loop_routes():
    async def scenario():
        committed = set()
        both_committed = asyncio.Event()
        calls = {'a': 0, 'c': 0, 'bad': 0}
        async def work(ctx):
            name = ctx.current_node
            calls[name] += 1
            if name == 'a':
                return {'round': calls[name]}
            if name == 'bad':
                raise AssertionError('superseded loop routed a stale result')
            return {'round': ctx.results['a']['round'], 'stale': calls[name] == 2}
        runner = runtime([
            node('a', next='c'),
            node('c', predecessors=['a'],
                 next=[{'node': 'bad', 'when': "ctx.results['c']['stale']"}],
                 goto=[{'node': 'a', 'when': "ctx.results['c']['round'] == 1"}],
                 loop={'when': "ctx.results['c']['round'] == 1"}),
            node('bad', predecessors=['c']),
        ], work, entry='a')
        commit = runner._commit_node
        async def coordinated(ctx, scope, name, generation):
            await commit(ctx, scope, name, generation)
            if name in ('a', 'c') and calls[name] == 2:
                committed.add(name)
                if committed == {'a', 'c'}:
                    both_committed.set()
                await asyncio.wait_for(both_committed.wait(), 1)
        runner._commit_node = coordinated
        result = await asyncio.wait_for(runner.run('main'), 2)
        assert result.success, result.error
        assert calls == {'a': 2, 'c': 3, 'bad': 0}
        assert result.context.results['c']['round'] == 2
    asyncio.run(scenario())


def test_predecessor_waits_for_already_started_loop_storage_commit(tmp_path):
    import threading

    class PausingStore:
        def __init__(self):
            self.db = SQLiteContextStore(tmp_path / 'inflight.db')
            self.started, self.release = threading.Event(), threading.Event()
        def load(self, session_id):
            return self.db.load(session_id)
        def save(self, ctx, *, expected_revision=None):
            value = ctx.results.get('c', {})
            if value.get('count') == 2 and not self.started.is_set():
                self.started.set()
                assert self.release.wait(3)
            return self.db.save(ctx, expected_revision=expected_revision)

    async def scenario():
        store = PausingStore()
        a_calls = 0
        async def work(ctx):
            nonlocal a_calls
            if ctx.current_node == 'a':
                a_calls += 1
                if a_calls == 2:
                    assert await asyncio.to_thread(store.started.wait, 2)
                return {'round': a_calls}
            count = ctx.previous.get('count', 0) + 1
            if count > 2 and ctx.results['a']['round'] == 1:
                await asyncio.Event().wait()
            return {'count': count, 'round': ctx.results['a']['round']}
        runner = runtime([
            node('a', next='c'),
            node('c', predecessors=['a'],
                 goto=[{'node': 'a', 'when': "ctx.results['c']['count'] == 1"}],
                 loop={'when': "ctx.results['c']['round'] == 1"}),
        ], work, entry='a')
        runner.context_store = store
        ctx = GraphContext(session_id=uuid4())
        task = asyncio.create_task(runner.run('main', ctx))
        try:
            assert await asyncio.to_thread(store.started.wait, 2)
            await asyncio.sleep(0.02)
            assert ctx.results['c']['count'] == 1  # disk commit not yet published
        finally:
            store.release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.success, result.error
        assert ctx.results['c']['round'] == 2
        assert ctx.results['c']['count'] >= 3
        saved = GraphContext.from_dict(store.load(ctx.session_id))
        assert saved.results['c'] == ctx.results['c']
    asyncio.run(scenario())


@pytest.mark.parametrize('duration', [0, 20, True, '', '20', '0ms', '-1s', 'nan', 'inf', '1Hz', '0.0s'])
def test_loop_period_rejects_ambiguous_or_nonpositive_durations(duration):
    with pytest.raises(ValueError, match='loop.every'):
        GraphNode.model_validate(node('start', loop={'every': duration}))


@pytest.mark.parametrize('duration,seconds', [('20ms', .02), ('0.5s', .5), ('.25s', .25)])
def test_loop_period_units(duration, seconds):
    parsed = GraphNode.model_validate(node('start', loop={'every': duration}))
    assert parsed.loop.interval_seconds() == seconds
    assert parsed.model_dump()['loop']['every'] == duration


def test_unreachable_default_warns_and_reachable_default_does_not():
    import warnings
    async def work(ctx):
        return {}
    for routes in ({'loop': True}, {'loop': {'when': 'true', 'every': '20ms'}}, {'goto': 'start'}):
        with pytest.warns(UserWarning, match='default is unreachable'):
            runtime([node('start', default='END', **routes)], work)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter('always')
        runtime([node('start', default='END', loop={'when': "ctx.inputs['active']"})], work)
    assert not recorded


def test_trigger_is_task_local_and_routes_can_exclude_self_loop():
    async def scenario():
        seen = []
        both = asyncio.Event()
        async def work(ctx):
            name, trigger = ctx.current_node, ctx.trigger
            seen.append((name, trigger))
            if name in ('worker', 'direct'):
                if {n for n, _ in seen} >= {'worker', 'direct'}:
                    both.set()
                await asyncio.wait_for(both.wait(), 1)
                assert ctx.trigger == trigger
            return {'count': ctx.previous.get('count', 0) + 1}
        runner = runtime([
            node('start', next={'node': 'worker', 'when': "ctx.trigger == 'entry'"},
                 goto={'node': 'direct', 'when': "ctx.trigger == 'entry'"},
                 loop={'every': '10ms', 'when': "ctx.results['start']['count'] < 3"}),
            node('worker', predecessors=['start']), node('direct'),
        ], work)
        result = await asyncio.wait_for(runner.run('main'), 2)
        assert result.success, result.error
        assert seen.count(('start', 'entry')) == 1
        assert seen.count(('start', 'loop')) == 2
        assert seen.count(('worker', 'next')) == 1
        assert seen.count(('direct', 'goto')) == 1
        assert result.context.trigger is None
    asyncio.run(scenario())


def test_periodic_loop_waits_without_blocking_normal_branch():
    async def scenario():
        times = []
        async def work(ctx):
            if ctx.current_node == 'other':
                assert len(times) == 1
                return {}
            times.append(asyncio.get_running_loop().time())
            return {'count': len(times)}
        runner = runtime([
            node('start', next={'node': 'other', 'when': "ctx.trigger == 'entry'"},
                 loop={'every': '30ms', 'when': "ctx.results['start']['count'] < 3"}),
            node('other', predecessors=['start']),
        ], work)
        result = await asyncio.wait_for(runner.run('main'), 2)
        assert result.success, result.error
        assert len(times) == 3
        assert times[-1] - times[0] >= .055
    asyncio.run(scenario())


def test_predecessor_replaces_sleeping_loop_without_waiting_for_timer():
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append((ctx.current_node, ctx.trigger))
            if ctx.current_node == 'a':
                return {'count': ctx.previous.get('count', 0) + 1}
            return {}
        runner = runtime([
            node('a', next='c'),
            node('c', predecessors=['a'],
                 goto={'node': 'a', 'when': "ctx.results['a']['count'] == 1"},
                 loop={'every': '60s', 'when': "ctx.results['a']['count'] == 1"}),
        ], work, entry='a')
        result = await asyncio.wait_for(runner.run('main'), 1)
        assert result.success, result.error
        assert seen == [('a', 'entry'), ('c', 'next'), ('a', 'goto'), ('c', 'next')]
    asyncio.run(scenario())


def test_goto_bypasses_unfinished_predecessors_on_first_call():
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append(ctx.current_node)
            return {}
        runner = runtime([
            node('start', goto='target'), node('unused', next='target'),
            node('target', predecessors=['unused']),
        ], work)
        result = await runner.run('main')
        assert result.success, result.error
        assert seen == ['start', 'target']
    asyncio.run(scenario())


def test_overrun_skips_missed_ticks_and_priority_clears_timer(monkeypatch):
    from types import SimpleNamespace
    from robotenon.workflow import scheduler
    clock = [10.055]
    monkeypatch.setattr(scheduler, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    async def work(ctx):
        return {}
    runner = runtime([node('start', loop={'every': '20ms'})], work)
    instance = scheduler.WaveScheduler(runner.compiled['main'], GraphContext(), '["main"]',
                                       None, None, None, None, None, None)
    instance.state['pending'].clear()
    instance.loop_anchors['start'] = 10.0
    instance._enqueue('start', 'loop')
    assert instance.not_before['start'] == pytest.approx(10.06)
    instance._enqueue('start', 'next')
    assert 'start' not in instance.not_before
    instance._enqueue('start', 'loop')
    assert instance.state['pending_triggers']['start'] == 'next'
    assert 'start' not in instance.not_before


def test_restore_pending_loop_rebases_clock_and_preserves_trigger():
    from robotenon.workflow.scheduler import WaveScheduler
    async def scenario():
        async def work(ctx):
            return {}
        runner = runtime([node('start', loop={'every': '20ms'})], work)
        ctx = GraphContext()
        first = WaveScheduler(runner.compiled['main'], ctx, '["main"]', None, None, None, None, None, None)
        first._enqueue('start', 'loop')
        restored_ctx = GraphContext.from_dict(ctx.to_dict())
        restored = WaveScheduler(runner.compiled['main'], restored_ctx, '["main"]', None, None, None, None, None, None)
        assert restored.state['pending_triggers']['start'] == 'loop'
        assert restored.not_before['start'] >= first.not_before['start']
    asyncio.run(scenario())
