"""Outer-AND, inner-OR predecessor groups."""

import asyncio

import pytest
from test_graph_parallel_routing import node, runtime

from robotenon.contracts.context import GraphContext
from robotenon.workflow.definition import GraphNode


def test_legacy_flat_predecessors_still_require_every_source():
    async def scenario():
        seen = []

        async def work(ctx):
            seen.append(ctx.current_node)
            return {}

        result = await runtime(
            [
                node("start", next="a"),
                node("a", predecessors=["start"], next="join"),
                node("b", next="join"),
                node("join", predecessors=["a", "b"]),
            ],
            work,
        ).run("main")
        assert result.success, result.error
        assert "join" not in seen

    asyncio.run(scenario())


def test_single_or_group_runs_once_when_either_source_arrives():
    async def scenario():
        seen = []

        async def work(ctx):
            seen.append(ctx.current_node)
            return {}

        result = await runtime(
            [
                node("start", next="a"),
                node("a", predecessors=["start"], next="join"),
                node("b", next="join"),
                node("join", predecessors=[["a", "b"]]),
            ],
            work,
        ).run("main")
        assert result.success, result.error
        assert seen.count("join") == 1

    asyncio.run(scenario())


def test_plain_source_and_or_group_must_both_be_satisfied():
    async def scenario():
        seen = []

        async def work(ctx):
            seen.append(ctx.current_node)
            return {}

        result = await runtime(
            [
                node("start", next=["a", "b"]),
                node("a", predecessors=["start"], next="join"),
                node("b", predecessors=["start"], next="join"),
                node("c", next="join"),
                node("join", predecessors=["a", ["b", "c"]]),
            ],
            work,
        ).run("main")
        assert result.success, result.error
        assert seen.count("join") == 1
        assert seen.index("join") > seen.index("a")
        assert seen.index("join") > seen.index("b")

    asyncio.run(scenario())


def test_two_or_groups_each_need_one_arrival():
    async def scenario():
        seen = []

        async def work(ctx):
            seen.append(ctx.current_node)
            return {}

        result = await runtime(
            [
                node("start", next=["a", "c"]),
                node("a", predecessors=["start"], next="join"),
                node("b", next="join"),
                node("c", predecessors=["start"], next="join"),
                node("d", next="join"),
                node("join", predecessors=[["a", "b"], ["c", "d"]]),
            ],
            work,
        ).run("main")
        assert result.success, result.error
        assert seen.count("join") == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("predecessors", "message"),
    [
        ([[]], "cannot be empty"),
        ([[["a"]]], "exactly two levels"),
        (["a", ["a", "b"]], "cannot repeat"),
    ],
)
def test_invalid_predecessor_group_shapes_are_rejected(predecessors, message):
    with pytest.raises(ValueError, match=message):
        GraphNode.model_validate(
            {"id": "target", "module": "work", "predecessors": predecessors}
        )


def test_unknown_or_group_member_is_rejected():
    async def work(_ctx):
        return {}

    with pytest.raises(ValueError, match="predecessor 'missing' is unknown"):
        runtime(
            [
                node("start", next="target"),
                node("target", predecessors=[["start", "missing"]]),
            ],
            work,
        )


def test_checkpoint_resume_preserves_or_and_arrival_tokens():
    async def scenario():
        seen = []
        fail = True

        async def work(ctx):
            nonlocal fail
            if ctx.current_node == "delay" and fail:
                raise RuntimeError("simulate interruption")
            seen.append(ctx.current_node)
            return {}

        runner = runtime(
            [
                node("start", next=["a", "branch"]),
                node("a", predecessors=["start"], next="join"),
                node("b", next="join"),
                node("branch", predecessors=["start"], next="delay"),
                node("delay", predecessors=["branch"], next="c"),
                node("c", predecessors=["delay"], next="join"),
                node("d", next="join"),
                node("join", predecessors=[["a", "b"], ["c", "d"]]),
            ],
            work,
        )
        first = await runner.run("main")
        assert not first.success
        restored = GraphContext.from_dict(first.context.to_dict())
        cursor = next(iter(restored.cursors.values()))
        assert cursor["arrivals"]["join"] == {"a": 1}

        fail = False
        second = await runner.run("main", restored)
        assert second.success, second.error
        assert seen.count("join") == 1

    asyncio.run(scenario())


def test_diagnostics_hide_unselected_members_of_satisfied_or_group():
    async def scenario():
        async def work(_ctx):
            return {}

        result = await runtime(
            [
                node("start", next="a"),
                node("a", predecessors=["start"], next="join"),
                node("b", next="join"),
                node("c", next="join"),
                node("join", predecessors=[["a", "b"], "c"]),
            ],
            work,
        ).run("main")
        assert result.success, result.error
        assert result.routing_diagnostics[0]["arrived"] == ["a"]
        assert set(result.routing_diagnostics[0]["missing"]) == {"c"}

    asyncio.run(scenario())
