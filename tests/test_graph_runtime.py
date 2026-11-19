import asyncio
import json
from uuid import uuid4

import pytest
from test_graph_parallel_routing import FunctionModule, node, runtime

from robotenon.runtime.config import ModuleConfig, ModulesConfig
from robotenon.runtime.factory import build_module_runtime
from robotenon.runtime.registry import register_module
from robotenon.envs import bind_environment, environment
from robotenon.schemas import Action, ActionType, EnvironmentFeedback
from robotenon.storage.context import SQLiteContextStore
from robotenon.workflow import GraphContext, GraphRuntime, evaluate_condition, load_graphs_config
from robotenon.workflow.definition import GraphDefinition


@register_module("graph-counter", kind="utility")
class GraphCounter:
    async def run(self, ctx: GraphContext):
        count = int(ctx.previous.get("count", ctx.results.get("child", {}).get("results", {}).get("increment", {}).get("count", 0))) + 1
        ctx.values["count"] = count
        return {"count": count}


@register_module("graph-route", kind="utility")
class GraphRoute:
    async def run(self, ctx: GraphContext):
        return {"route": "done" if ctx.results["child"]["results"]["increment"]["count"] >= 2 else "loop"}


@register_module("graph-action", kind="policy")
class GraphAction:
    async def run(self, ctx: GraphContext):
        ctx.action = Action(session_id=ctx.session_id, action_type=ActionType.WAIT)
        return {"action_ready": True}


@register_module("graph-parallel-a", kind="utility")
class GraphParallelA:
    async def run(self, ctx: GraphContext):
        await asyncio.sleep(0.01)
        ctx.values["a_done"] = True
        return {"a_done": True}


@register_module("graph-parallel-b", kind="utility")
class GraphParallelB:
    async def run(self, ctx: GraphContext):
        await asyncio.sleep(0.02)
        ctx.values["b_done"] = True
        return {"b_done": True}


@register_module("graph-join", kind="utility")
class GraphJoin:
    async def run(self, ctx: GraphContext):
        # This node must not start after only one branch has completed.
        assert ctx.results["a"].get("a_done") is True
        assert ctx.results["b"].get("b_done") is True
        ctx.values["joined"] = True
        return {"joined": True}


class GraphEnvironment:
    def __init__(self):
        from uuid import uuid4

        self.session_id = uuid4()
        self.reset_context = None
        self.actions = []
        self.closed = False

    async def reset(self, ctx: GraphContext):
        self.reset_context = ctx
        return EnvironmentFeedback(session_id=self.session_id, sequence_no=0)

    async def execute_action(self, action: Action):
        self.actions.append(action)
        return EnvironmentFeedback(
            session_id=self.session_id,
            sequence_no=len(self.actions),
            action_id=action.action_id,
            action_status="succeeded",
        )

    async def close(self):
        self.closed = True


def test_json_graph_supports_condition_loop_and_nested_graph(tmp_path):
    path = tmp_path / "graphs.json"
    path.write_text(
        json.dumps(
            {
                "graphs": [
                    {'id': 'main', 'entry': 'child', 'nodes': [{'id': 'child', 'graph': 'counter', 'next': [{'node': 'route'}]}, {'id': 'route', 'module': 'graph-route', 'predecessors': ['child'], 'next': [{'node': 'done', 'when': "ctx.results['route']['route'] == 'done'"}], 'goto': [{'node': 'child', 'when': "ctx.results['route']['route'] == 'loop'"}]}, {'id': 'done', 'module': 'graph-counter', 'predecessors': ['route'], 'next': [{'node': 'END'}]}]},
                    {'id': 'counter', 'entry': 'increment', 'max_steps': 10, 'backend': 'fallback', 'nodes': [{'id': 'increment', 'module': 'graph-counter', 'loop': {'when': "ctx.results['increment']['count'] < 2"}, 'next': [{'node': 'END'}]}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    graphs = load_graphs_config(path)
    runtime = GraphRuntime(
        graphs.graph_map(),
        build_module_runtime(
            ModulesConfig(
                modules=[
                    ModuleConfig(name="graph-counter"),
                    ModuleConfig(name="graph-route"),
                ]
            )
        ),
        backend="fallback",
    )

    async def scenario():
        ctx = GraphContext()
        result = await runtime.run("main", ctx)
        await runtime.modules.close()
        return result

    result = asyncio.run(scenario())
    assert result.success is True
    assert result.status == "completed"
    assert result.context.results["done"]["count"] == 3
    assert result.context.outputs["child"]["success"] is True
    assert result.context.current_node == "done"


def test_graph_runtime_accepts_optional_event_callback():
    graph = GraphDefinition.model_validate(
        {
            "id": "callback-graph",
            "entry": "increment",
            "nodes": [{"id": "increment", "module": "graph-counter"}],
        }
    )
    runtime = GraphRuntime(
        {graph.id: graph},
        build_module_runtime(
            ModulesConfig(modules=[ModuleConfig(name="graph-counter")])
        ),
        backend="fallback",
        event_callback=lambda ctx, event, result=None: events.append(
            (event["event"], event["node"], result is not None)
        ),
    )
    events = []

    async def scenario():
        result = await runtime.run("callback-graph", ctx=GraphContext())
        await runtime.modules.close()
        return result

    result = asyncio.run(scenario())

    assert result.success is True
    assert [(event, node) for event, node, _ in events] == [
        ("enter", "increment"),
        ("exit", "increment"),
    ]
    assert events[-1][2] is False


def test_condition_evaluator_is_safe_and_handles_context_values():
    ctx = GraphContext(values={"mode": "safe", "n": 3})
    assert evaluate_condition("ctx.values['mode'] == 'safe' and ctx.values['n'] >= 2", ctx)
    assert evaluate_condition("ctx.values['mode'] in ['safe', 'test']", ctx)
    with pytest.raises(ValueError, match="unsupported expression"):
        evaluate_condition("__import__('os').system('echo bad')", ctx)


def test_graph_manifest_rejects_unknown_edges(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "graphs": [
                    {
                        "id": "bad",
                        "entry": "a",
                        "nodes": [{"id": "a", "module": "graph-counter"}],
                        "edges": [{"from": "a", "to": "missing"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="target 'missing' is unknown"):
        load_graphs_config(path)


def test_nested_failure_is_reported_for_parent_graph_and_loop_is_bounded():
    graph_values = {
        "parent": {
            "id": "parent",
            "entry": "child",
            "nodes": [{"id": "child", "graph": "child", "next": [{"node": "END"}]}],
        },
        "child": {'id': 'child', 'entry': 'loop', 'max_steps': 2, 'backend': 'fallback', 'nodes': [{'id': 'loop', 'module': 'graph-counter', 'loop': True}]},
    }
    from robotenon.workflow.definition import GraphDefinition

    runtime = GraphRuntime(
        {key: GraphDefinition.model_validate(value) for key, value in graph_values.items()},
        build_module_runtime(ModulesConfig(modules=[ModuleConfig(name="graph-counter")])),
        backend="fallback",
    )

    async def scenario():
        result = await runtime.run("parent")
        await runtime.modules.close()
        return result

    result = asyncio.run(scenario())
    assert result.success is False
    assert result.graph_id == "parent"
    assert result.status == "max_steps"


def test_transition_replaces_graph_and_skips_current_graph_successors():
    async def scenario():
        seen = []
        context_ids = []

        async def work(ctx):
            seen.append((ctx.current_graph, ctx.current_node))
            context_ids.append(id(ctx))
            return {"visited": ctx.current_node}

        target = GraphDefinition.model_validate(
            {"id": "target", "entry": "entry", "nodes": [node("entry")]}
        )
        runner = runtime(
            [
                node(
                    "switch",
                    default="tail",
                    transition={
                        "graph": "target",
                        "when": "ctx.inputs['go']",
                    },
                ),
                node("tail", predecessors=["switch"]),
            ],
            work,
            entry="switch",
            graphs=[target],
        )
        ctx = GraphContext(values={"go": True})
        result = await runner.run("main", ctx)
        return result, ctx, seen, context_ids

    result, ctx, seen, context_ids = asyncio.run(scenario())
    assert result.success, result.error
    assert result.graph_id == "target"
    assert seen == [("main", "switch"), ("target", "entry")]
    assert len(set(context_ids)) == 1 and context_ids[0] == id(ctx)
    assert ctx.root_scope == '["target"]'
    assert ctx.active_graph is None
    assert any(item.get("event") == "transition" for item in ctx.history)


def test_transition_can_cycle_between_graphs_without_recursive_invocation():
    async def scenario():
        seen = []

        async def work(ctx):
            count = int(ctx.inputs.get("count", 0)) + 1
            seen.append((ctx.current_graph, count))
            return {"count": count}

        graph_a = GraphDefinition.model_validate(
            {
                "id": "a",
                "entry": "step",
                "nodes": [
                    node(
                        "step",
                        transition={
                            "graph": "b",
                            "inputs": {"count": "nodes.step.outputs.count"},
                        },
                    )
                ],
            }
        )
        graph_b = GraphDefinition.model_validate(
            {
                "id": "b",
                "entry": "step",
                "nodes": [
                    node(
                        "step",
                        transition={
                            "graph": "a",
                            "when": "ctx.results['step']['count'] < 4",
                            "inputs": {"count": "nodes.step.outputs.count"},
                        },
                    )
                ],
            }
        )
        from robotenon.modules import ModuleRuntime
        from robotenon.runtime.safety import SafetyStateStore
        runner = GraphRuntime(
            [graph_a, graph_b],
            ModuleRuntime({"work": FunctionModule(work)}),
            safety_state=SafetyStateStore(),
            backend="fallback",
        )
        result = await runner.run("a", initial_values={"count": 0})
        return result, seen

    result, seen = asyncio.run(scenario())
    assert result.success, result.error
    assert result.graph_id == "b"
    assert seen == [("a", 1), ("b", 2), ("a", 3), ("b", 4)]
    assert "recursive graph invocation detected" not in str(result.error)


def test_transition_target_survives_checkpoint_before_target_entry(tmp_path):
    async def scenario():
        seen = []

        async def work(ctx):
            seen.append((ctx.current_graph, ctx.current_node))
            return {"ok": True}

        target = GraphDefinition.model_validate(
            {"id": "target", "entry": "entry", "nodes": [node("entry")]}
        )
        runner = runtime(
            [node("switch", transition={"graph": "target"})],
            work,
            entry="switch",
            graphs=[target],
        )
        runner.context_store = SQLiteContextStore(tmp_path / "transition.db")
        persist = runner._persist_context_async
        crashed = False

        async def crash_after_transition(ctx):
            nonlocal crashed
            await persist(ctx)
            if ctx.transition_pending and not crashed:
                crashed = True
                raise RuntimeError("crash after transition checkpoint")

        runner._persist_context_async = crash_after_transition
        ctx = GraphContext(session_id=uuid4())
        first = await runner.run("main", ctx)
        assert not first.success

        restored = GraphContext.from_dict(runner.context_store.load(ctx.session_id))
        assert restored.active_graph == "target"
        assert restored.transition_pending is True
        runner._persist_context_async = persist
        second = await runner.run("main", restored)
        return second, seen

    result, seen = asyncio.run(scenario())
    assert result.success, result.error
    assert result.graph_id == "target"
    assert seen == [("main", "switch"), ("target", "entry")]


def test_system_environment_nodes_receive_shared_ctx_and_env():
    from robotenon.workflow.definition import GraphDefinition

    graph = GraphDefinition.model_validate(
        {
            "id": "embodied",
            "entry": "reset",
            "backend": "fallback",
            "nodes": [
                {"id": "reset", "type": "system", "operation": "env.reset", "next": [{"node": "policy"}]},
                {"id": "policy", "type": "module", "module": "graph-action", "predecessors": ["reset"], "next": [{"node": "body"}]},
                {"id": "body", "type": "system", "operation": "body.execute", "predecessors": ["policy"], "next": [{"node": "close"}]},
                {"id": "close", "type": "system", "operation": "env.close", "predecessors": ["body"], "next": [{"node": "END"}]},
            ],
        }
    )
    modules = build_module_runtime(ModulesConfig(modules=[ModuleConfig(name="graph-action")]))
    env = GraphEnvironment()
    from robotenon.runtime.safety import SafetyStateStore
    from robotenon.services import InProcessBodyService

    safety = SafetyStateStore()
    body = InProcessBodyService(env.execute_action, safety_state=safety)

    async def scenario():
        ctx = GraphContext()
        runtime = GraphRuntime(
            {"embodied": graph}, modules, backend="fallback", safety_state=safety
        )
        previous = bind_environment(env)
        try:
            result = await runtime.run("embodied", ctx=ctx, body=body)
        finally:
            environment.restore(previous)
            await body.close()
            await modules.close()
        return result, ctx

    result, ctx = asyncio.run(scenario())
    assert result.success is True
    assert env.reset_context is ctx
    assert len(env.actions) == 1
    assert ctx.feedback is not None and ctx.feedback.sequence_no == 1
    assert env.closed is True


def test_graph_context_can_round_trip_without_environment_handle():
    ctx = GraphContext(values={"score": 0.9}, task_id="pick", step=3)
    restored = GraphContext.from_dict(ctx.to_dict())
    assert restored.values == {"score": 0.9}
    assert restored.task_id == "pick"
    assert restored.step == 3
    assert "env" not in restored.to_dict()


def test_graph_context_persists_and_restores_through_shared_store(tmp_path):
    from uuid import uuid4

    from robotenon.core.context import ContextConflictError, SQLiteContextStore

    store = SQLiteContextStore(tmp_path / "context.sqlite3")
    session_id = uuid4()
    ctx = GraphContext(session_id=session_id, values={"phase": "policy"})
    assert store.save(ctx) == 1
    restored = GraphContext.from_dict(store.load(session_id))
    assert restored.values["phase"] == "policy"
    assert restored.context_revision == 1
    stale = GraphContext(session_id=session_id, values={"phase": "stale"})
    with pytest.raises(ContextConflictError):
        store.save(stale)


def test_predecessors_run_parallel_and_join_after_all_dependencies():
    from robotenon.workflow.definition import GraphDefinition

    graph = GraphDefinition.model_validate(
        {
            "id": "parallel",
            "entry": "start",
            "backend": "fallback",
            "nodes": [
                {'id': 'start', 'module': 'graph-counter', 'next': [{'node': 'a'}, {'node': 'b'}]},
                {'id': 'a', 'module': 'graph-parallel-a', 'predecessors': ['start'], 'next': [{'node': 'join'}]},
                {'id': 'b', 'module': 'graph-parallel-b', 'predecessors': ['start'], 'next': [{'node': 'join'}]},
                {'id': 'join', 'module': 'graph-join', 'predecessors': ['a', 'b'], 'next': [{'node': 'END'}]},
            ],
        }
    )
    modules = build_module_runtime(
        ModulesConfig(
            modules=[
                ModuleConfig(name="graph-counter"),
                ModuleConfig(name="graph-parallel-a"),
                ModuleConfig(name="graph-parallel-b"),
                ModuleConfig(name="graph-join"),
            ]
        )
    )

    async def scenario():
        result = await GraphRuntime({"parallel": graph}, modules, backend="fallback").run("parallel")
        await modules.close()
        return result

    result = asyncio.run(scenario())
    assert result.success is True
    assert result.context.results["join"]["joined"] is True
    assert result.context.step == 4


def test_goto_statement_supports_a_bounded_loop():
    from robotenon.workflow.definition import GraphDefinition

    graph = GraphDefinition.model_validate(
        {
            "id": "goto-loop",
            "entry": "loop",
            "backend": "fallback",
            "nodes": [
                {'id': 'loop', 'module': 'graph-counter', 'default': 'END', 'predecessors': [], 'goto': [{'node': 'loop', 'when': "ctx.results['loop']['count'] < 3"}]}
            ],
            "max_steps": 10,
        }
    )
    modules = build_module_runtime(ModulesConfig(modules=[ModuleConfig(name="graph-counter")]))

    async def scenario():
        result = await GraphRuntime({"goto-loop": graph}, modules, backend="fallback").run("goto-loop")
        await modules.close()
        return result

    result = asyncio.run(scenario())
    assert result.success is True
    assert result.context.results["loop"]["count"] == 3
    assert result.context.step == 3


def test_emergency_and_level_specific_recovery_share_context_and_environment():
    from robotenon.runtime.safety import SafetyStateStore
    from robotenon.schemas import SafetyLevel
    from robotenon.services import InProcessBodyService
    from robotenon.envs.base.fake import FakeEnvironmentAdapter
    from robotenon.schemas import SessionSpec
    from robotenon.workflow.definition import GraphDefinition

    graph = GraphDefinition.model_validate(
        {
            "id": "main-recovery",
            "entry": "reset",
            "backend": "fallback",
            "max_steps": 10,
            "emergency_graphs": {"P3": "pause"},
            "recovery": {
                "P3": {
                    "mode": "manual",
                    "recovery_graph": "recover",
                    "resume_graph": "resume",
                    "clear_latched": True,
                }
            },
            "nodes": [
                {'id': 'reset', 'type': 'system', 'operation': 'env.reset', 'next': [{'node': 'END'}]}
            ],
        }
    )
    pause = GraphDefinition.model_validate(
        {
            "id": "pause",
            "entry": "interrupt",
            "backend": "fallback",
            "nodes": [
                {'id': 'interrupt', 'type': 'system', 'operation': 'body.interrupt', 'next': [{'node': 'END'}]}
            ],
        }
    )
    recover = GraphDefinition.model_validate(
        {
            "id": "recover",
            "entry": "snapshot",
            "backend": "fallback",
            "nodes": [
                {'id': 'snapshot', 'type': 'system', 'operation': 'safety.snapshot', 'next': [{'node': 'END'}]}
            ],
        }
    )
    resume = GraphDefinition.model_validate(
        {
            "id": "resume",
            "entry": "snapshot",
            "backend": "fallback",
            "nodes": [
                {'id': 'snapshot', 'type': 'system', 'operation': 'safety.snapshot', 'next': [{'node': 'END'}]}
            ],
        }
    )
    modules = build_module_runtime(ModulesConfig(modules=[]))

    async def scenario():
        adapter = FakeEnvironmentAdapter(max_steps=1)
        session = await adapter.open(SessionSpec(task_id="recover", environment_id="fake"))
        env = session
        store = SafetyStateStore()
        body = InProcessBodyService(lambda action: None, safety_state=store)
        runtime = GraphRuntime(
            {item.id: item for item in (graph, pause, recover, resume)},
            modules,
            backend="fallback",
            safety_state=store,
        )
        store.raise_level(SafetyLevel.P3, code="test.pause")
        ctx = GraphContext(task_id="recover")
        previous = bind_environment(env)
        try:
            stopped = await runtime.run("main-recovery", ctx=ctx, body=body)
            assert stopped.status == "safety_stop", stopped.error
            assert stopped.context is ctx
            waiting = await runtime.recover("main-recovery", ctx, body=body)
            assert waiting.status == "recovery_required"
            resumed = await runtime.recover("main-recovery", ctx, body=body, approved=True)
            assert resumed.success is True
            assert resumed.context is ctx
            assert resumed.context.safety.level is SafetyLevel.P0
        finally:
            environment.restore(previous)
            await body.close()
            await env.close()

    asyncio.run(scenario())


def test_recovery_controller_waits_for_manual_approval_then_resumes():
    from robotenon.runtime.safety import SafetyStateStore
    from robotenon.runtime.config import ModulesConfig
    from robotenon.runtime.factory import build_module_runtime
    from robotenon.schemas import SafetyLevel
    from robotenon.workflow import RecoveryController
    from robotenon.workflow.definition import GraphDefinition

    main = GraphDefinition.model_validate(
        {
            "id": "controller-main",
            "entry": "snapshot",
            "backend": "fallback",
            "recovery": {
                "P3": {
                    "mode": "manual",
                    "recovery_graph": "controller-recover",
                    "resume_graph": "controller-resume",
                    "clear_latched": True,
                }
            },
            "nodes": [{'id': 'snapshot', 'type': 'system', 'operation': 'safety.snapshot', 'next': [{'node': 'END'}]}],
        }
    )
    recover = GraphDefinition.model_validate(
        {
            "id": "controller-recover",
            "entry": "snapshot",
            "backend": "fallback",
            "nodes": [{'id': 'snapshot', 'type': 'system', 'operation': 'safety.snapshot', 'next': [{'node': 'END'}]}],
        }
    )
    resume = GraphDefinition.model_validate(
        {
            "id": "controller-resume",
            "entry": "snapshot",
            "backend": "fallback",
            "nodes": [{'id': 'snapshot', 'type': 'system', 'operation': 'safety.snapshot', 'next': [{'node': 'END'}]}],
        }
    )

    async def scenario():
        store = SafetyStateStore()
        modules = build_module_runtime(ModulesConfig(modules=[]))
        runtime = GraphRuntime(
            {item.id: item for item in (main, recover, resume)},
            modules,
            backend="fallback",
            safety_state=store,
        )
        store.raise_level(SafetyLevel.P3, code="controller.blocked")
        ctx = GraphContext(status="safety_stop")
        controller = RecoveryController(runtime, "controller-main", ctx, safety_state=store, poll_hz=100)
        assert await controller.run_once() is None
        assert controller.snapshot.status == "waiting_manual"
        await controller.approve()
        result = await controller.run_once()
        assert result is not None and result.success is True
        assert controller.snapshot.status == "completed"
        await modules.close()

    asyncio.run(scenario())


def test_default_p4_emergency_graph_keeps_resident_environment_open():
    graphs = load_graphs_config("configs/graphs.json")
    emergency_stop = graphs.graph_map()["emergency_stop"]
    assert all(node.operation != "env.close" for node in emergency_stop.nodes)
    assert any(node.operation == "body.interrupt" for node in emergency_stop.nodes)
