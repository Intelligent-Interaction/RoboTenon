"""Conditional fan-out, strict joins, resumable tokens and cooperative ctx writes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from robotenon.contracts.module import NodeResult
from robotenon.modules import ModuleRuntime
from robotenon.runtime.safety import SafetyStateStore
from robotenon.workflow import GraphContext, GraphRuntime
from robotenon.workflow.definition import GraphDefinition


class FunctionModule:
    def __init__(self, function):
        self.function = function

    async def run(self, ctx):
        return await self.function(ctx)


def runtime(nodes, function, *, entry="start", graphs=()):
    graph = GraphDefinition.model_validate({"id": "main", "entry": entry, "nodes": nodes})
    return GraphRuntime(
        [graph, *graphs], ModuleRuntime({"work": FunctionModule(function)}),
        safety_state=SafetyStateStore(), backend="fallback",
    )


def node(name, **kwargs):
    return {"id": name, "module": "work", **kwargs}


@pytest.mark.parametrize("x,y,expected", [
    (True, True, {"start", "a", "b", "join"}),
    (True, False, {"start", "a"}),
    (False, True, {"start", "b"}),
    (False, False, {"start", "fallback"}),
])
def test_all_matching_edges_and_exclusive_default(x, y, expected):
    async def scenario():
        seen = set()
        both_started = asyncio.Event()

        async def work(ctx):
            seen.add(ctx.current_node)
            if {"a", "b"} <= seen:
                both_started.set()
            if x and y and ctx.current_node in {"a", "b"}:
                # Would time out if sibling nodes executed serially.
                await asyncio.wait_for(both_started.wait(), 1)
            return NodeResult(output=ctx.current_node)

        runner = runtime([
            node('start', default='fallback', next=[{'node': 'a', 'when': "ctx.inputs['x']"}, {'node': 'b', 'when': "ctx.inputs['y']"}]),
            node('a', predecessors=['start'], next=[{'node': 'join'}]),
            node('b', predecessors=['start'], next=[{'node': 'join'}]),
            node("join", predecessors=["a", "b"]),
            node("fallback", predecessors=["start"]),
        ], work)
        result = await runner.run("main", initial_values={"x": x, "y": y})
        assert result.success, result.error
        assert seen == expected
    asyncio.run(scenario())


@pytest.mark.parametrize("end_branch", [False, True])
def test_branch_without_successor_or_end_does_not_stop_siblings(end_branch):
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append(ctx.current_node)
            return NodeResult()
        runner = runtime([
            node('start', next=[{'node': 'a'}, {'node': 'b'}]),
            node('a', predecessors=['start'], next=[{'node': 'END'}] if end_branch else [{'node': 'blocked', 'when': 'false'}]),
            node('b', predecessors=['start'], next=[{'node': 'tail'}]),
            node("tail", predecessors=["b"]),
            *([] if end_branch else [node("blocked", predecessors=["a"])]),
        ], work)
        result = await runner.run("main")
        assert result.success, result.error
        assert seen == ["start", "a", "b", "tail"]
    asyncio.run(scenario())


def test_completed_predecessor_without_selected_edge_does_not_release_join():
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append(ctx.current_node)
            return NodeResult()
        runner = runtime([
            node('start', next=[{'node': 'a'}, {'node': 'b'}]),
            node('a', predecessors=['start'], next=[{'node': 'join'}]),
            node('b', predecessors=['start'], next=[{'node': 'join', 'when': 'false'}]),
            node("join", predecessors=["a", "b"]),
        ], work)
        result = await runner.run("main")
        assert result.success
        assert seen == ["start", "a", "b"]
    asyncio.run(scenario())


@pytest.mark.parametrize("nodes", [
    [node('start', next=[{'node': 'target'}]), node("target")],
    [node("start"), node("target", predecessors=["start"])],
    [node("start", default="target"), node("target")],
])
def test_half_declared_edges_are_rejected(nodes):
    async def work(ctx):
        return NodeResult()
    with pytest.raises(ValueError, match="requires"):
        runtime(nodes, work)


def test_join_request_and_completions_survive_checkpoint_once():
    async def scenario():
        seen = []
        fail = True
        async def work(ctx):
            if ctx.current_node == "delay" and fail:
                raise RuntimeError("simulate interruption")
            seen.append(ctx.current_node)
            return NodeResult()
        runner = runtime([
            node('start', next=[{'node': 'a'}, {'node': 'b'}]),
            node('a', predecessors=['start'], next=[{'node': 'join'}, {'node': 'join'}]),
            node('b', predecessors=['start'], next=[{'node': 'delay'}]),
            node('delay', predecessors=['b'], next=[{'node': 'join'}]),
            node("join", predecessors=["a", "delay"]),
        ], work)
        first = await runner.run("main")
        assert not first.success
        restored = GraphContext.from_dict(first.context.to_dict())
        cursor = next(iter(restored.cursors.values()))
        assert cursor["waiting"] == ["join"]
        assert cursor["arrivals"]["join"]["a"] == 1
        fail = False
        second = await runner.run("main", restored)
        assert second.success, second.error
        assert seen == ["start", "a", "b", "delay", "join"]
    asyncio.run(scenario())


def test_parallel_nested_graphs_own_independent_values():
    async def scenario():
        async def work(ctx):
            return NodeResult(state_patch={"shared": 1})
        child = GraphDefinition.model_validate({"id": "child", "entry": "work", "nodes": [node("work")]})
        runner = runtime([
            {"id": "left", "graph": "child"},
            {"id": "right", "graph": "child"},
        ], work, entry=["left", "right"], graphs=[child])
        result = await runner.run("main")
        assert result.success, result.error
        assert result.context.results["left"]["results"]["work"]["shared"] == 1
        assert result.context.results["right"]["results"]["work"]["shared"] == 1
    asyncio.run(scenario())


def test_many_parallel_patch_writers_with_distinct_keys():
    async def scenario():
        count = 128
        started = 0
        gate = asyncio.Event()
        async def work(ctx):
            nonlocal started
            started += 1
            if started == count:
                gate.set()
            await asyncio.wait_for(gate.wait(), 2)
            return NodeResult(state_patch={ctx.current_node: True})
        names = [f"node_{i}" for i in range(count)]
        result = await runtime([node(name) for name in names], work, entry=names).run("main")
        assert result.success, result.error
        assert all(result.context.results[name][name] for name in names)
    asyncio.run(scenario())


def test_atomic_updates_from_threads_and_detached_snapshots():
    ctx = GraphContext(values={"count": 0, "nested": {"items": []}})
    def increment(_):
        for _ in range(500):
            ctx.atomic_update("count", lambda value: value + 1)
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(increment, range(32)))
    assert ctx.snapshot_values("count") == {"count": 16000}
    snapshot = ctx.snapshot_values()
    snapshot["nested"]["items"].append("detached")
    assert ctx.values["nested"]["items"] == []
    def fail(value):
        value["items"].append("discarded")
        raise ValueError("abort")
    with pytest.raises(ValueError, match="abort"):
        ctx.atomic_update("nested", fail)
    assert ctx.values["nested"]["items"] == []
    async def invalid(value):
        return value
    with pytest.raises(TypeError, match="synchronous"):
        ctx.atomic_update("count", invalid)
    assert GraphContext.from_dict(ctx.to_dict()).values == ctx.values


@pytest.mark.parametrize("spelling", ["statement", "field"])
def test_loop_reenters_without_predecessor_and_clears_old_join_tokens(spelling):
    async def scenario():
        seen = []
        round_number = 0
        async def work(ctx):
            nonlocal round_number
            name = ctx.current_node
            if name == "fork":
                round_number += 1
                ctx.update(round=round_number)
            seen.append((name, round_number))
            return NodeResult()
        back = {'goto': 'goto fork'} if spelling == "statement" else {'goto': [{'node': 'fork'}]}
        runner = runtime([
            node('start', next=[{'node': 'fork'}]),
            node('fork', predecessors=['start'], next=[{'node': 'a'}, {'node': 'b'}]),
            node('a', predecessors=['fork'], next=[{'node': 'join'}]),
            node('b', predecessors=['fork'], next=[{'node': 'join', 'when': "ctx.results['fork']['round'] == 1"}]),
            node("join", predecessors=["a", "b"], **back),
        ], work)
        result = await runner.run("main")
        assert result.success, result.error
        assert seen.count(("start", 0)) == 1
        assert ("fork", 2) in seen
        assert [item for item in seen if item[0] == "join"] == [("join", 1)]
    asyncio.run(scenario())


def test_goto_can_call_a_never_executed_node():
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append((ctx.current_node, ctx.trigger))
            return NodeResult()
        result = await runtime([node('start', goto=[{'node': 'future'}]), node("future")], work).run("main")
        assert result.success, result.error
        assert seen == [('start', 'entry'), ('future', 'goto')]
    asyncio.run(scenario())


def test_matching_loop_suppresses_default_and_keeps_max_steps_bound():
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append(ctx.current_node)
            return NodeResult()
        runner = runtime([
            node('start', default='fallback', goto=[{'node': 'start'}]),
            node("fallback", predecessors=["start"]),
        ], work)
        runner.compiled["main"].definition.max_steps = 3
        result = await runner.run("main")
        assert result.status == "max_steps"
        assert seen == ["start"] * 3
    asyncio.run(scenario())


def test_false_loop_when_uses_default():
    async def scenario():
        seen = []
        async def work(ctx):
            seen.append(ctx.current_node)
            return NodeResult()
        result = await runtime([
            node('start', default='fallback', goto=[{'node': 'start', 'when': "ctx.inputs['retry']"}]),
            node("fallback", predecessors=["start"]),
        ], work).run("main", initial_values={"retry": False})
        assert result.success
        assert seen == ["start", "fallback"]
    asyncio.run(scenario())


def test_matching_next_and_goto_execute_in_parallel():
    async def scenario():
        started = asyncio.Event()
        seen = []
        async def work(ctx):
            seen.append(ctx.current_node)
            if ctx.current_node == "start":
                count = ctx.atomic_update("count", lambda n: n + 1, default=ctx.previous.get("count", 0))
                if count == 2:
                    await asyncio.wait_for(started.wait(), 1)
            else:
                started.set()
            return NodeResult()
        result = await runtime([
            node('start', default='END', next=[{'node': 'tail', 'when': "ctx.results['start']['count'] < 2"}], goto=[{'node': 'start', 'when': "ctx.results['start']['count'] < 2"}]),
            node("tail", predecessors=["start"]),
        ], work).run("main")
        assert result.success, result.error
        assert seen[0] == "start"
        assert sorted(seen[1:]) == ["start", "tail"]
    asyncio.run(scenario())
