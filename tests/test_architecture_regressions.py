"""Fault and concurrency contracts from the independent architecture audit."""

import asyncio
import json
from uuid import uuid4

import pytest

from robotenon.core.context import SQLiteContextStore
from robotenon.contracts.checkpoint import RunCheckpoint
from robotenon.modules import ModuleRuntime
from robotenon.runtime.safety import (
    SafetyStateStore,
    SQLiteSafetyStateStore,
    reconcile_safety_snapshot,
)
from robotenon.schemas import EnvironmentFeedback, EnvironmentSignal, SafetyLevel, SafetySignal
from robotenon.workflow import GraphContext, GraphRuntime
from robotenon.workflow.definition import GraphDefinition
from robotenon.services.ipc import BodyServiceClient, JsonRpcClient, JsonRpcServer


class Fn:
    def __init__(self, fn):
        self.fn = fn

    async def run(self, ctx):
        return await self.fn(ctx)


async def noop(ctx):
    return {}


def graph(name, nodes, **kwargs):
    return GraphDefinition.model_validate(
        {"id": name, "entry": nodes[0]["id"], "nodes": nodes, **kwargs}
    )


def runtime(graphs, modules, **kwargs):
    return GraphRuntime(
        graphs,
        ModuleRuntime({k: Fn(v) for k, v in modules.items()}),
        safety_state=SafetyStateStore(),
        **kwargs,
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("initial,observed", [("P3", "P1"), ("P3", "P0"), ("P4", "P3")])
def test_latch_requires_explicit_recovery(tmp_path, backend, initial, observed):
    store = (
        SafetyStateStore()
        if backend == "memory"
        else SQLiteSafetyStateStore(tmp_path / "safety.db")
    )
    store.raise_level(SafetyLevel(initial), code="monitor.compliance")
    incident = store.snapshot().incident_id
    store.raise_level(SafetyLevel(observed), code="monitor.compliance")
    snapshot = store.snapshot()
    assert snapshot.level == SafetyLevel(initial)
    assert snapshot.signals[0].latched
    assert snapshot.incident_id == incident
    with pytest.raises(PermissionError):
        store.recover()
    assert store.recover(approved=True).level == SafetyLevel.P0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_same_code_from_another_source_cannot_lower_latched_incident(tmp_path, backend):
    store = (
        SafetyStateStore()
        if backend == "memory"
        else SQLiteSafetyStateStore(tmp_path / "safety-same-code.db")
    )
    store.raise_level(SafetyLevel.P3, code="shared.collision", source="monitor-a")
    store.raise_level(SafetyLevel.P1, code="shared.collision", source="monitor-b")
    snapshot = store.snapshot()
    assert snapshot.level is SafetyLevel.P3
    assert snapshot.signals[0].latched


def test_monitor_runtime_restores_singleton_after_observation_decoration():
    """A temporary monitor binding must remove its auto-installed wrapper."""

    from robotenon.envs import environment
    from robotenon.infra.observation_runtime import (
        clear_observation_infrastructure,
        configure_observation_infrastructure,
    )
    from robotenon.monitors import MonitorRuntime
    from robotenon.schemas import EnvironmentFeedback, MonitorReport

    class Pipeline:
        async def ingest(self, value, **kwargs):
            return value

    class Service:
        async def observe(self):
            return EnvironmentFeedback(session_id=uuid4())

        async def health(self):
            return {"ready": True}

    class Check:
        async def check(self, feedback=None, **kwargs):
            return MonitorReport(compliant=True, safety_level=SafetyLevel.P0)

    async def scenario():
        previous_environment = environment.current
        cache = object()
        configure_observation_infrastructure(cache, pipeline=Pipeline())
        runtime = MonitorRuntime(
            {"check": Check()},
            modes={"check": {"resident"}},
            environment_service=Service(),
            safety_state=SafetyStateStore(),
            poll_hz=1000,
        )
        try:
            await runtime.start()
            assert getattr(environment.current, "service", None) is runtime._environment_service.service
        finally:
            await runtime.close()
            clear_observation_infrastructure(cache)
            assert environment.current is previous_environment

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_monitor_reconcile_removes_stale_transient_but_keeps_latched(tmp_path, backend):
    store = (
        SafetyStateStore()
        if backend == "memory"
        else SQLiteSafetyStateStore(tmp_path / "safety-reconcile.db")
    )
    store.reconcile(
        "camera-monitor",
        [
            SafetySignal(
                code="camera-monitor.occlusion",
                source="camera-monitor",
                level=SafetyLevel.P1,
            ),
            SafetySignal(
                code="camera-monitor.human_collision",
                source="camera-monitor",
                level=SafetyLevel.P3,
            ),
        ],
    )
    recovered = store.reconcile("camera-monitor", [])
    assert "camera-monitor.occlusion" not in {item.code for item in recovered.signals}
    assert "camera-monitor.human_collision" in {item.code for item in recovered.signals}
    assert recovered.level is SafetyLevel.P3
    assert store.reconcile("camera-monitor", [], clear_latched=True).level is SafetyLevel.P0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_environment_feedback_does_not_clear_other_owner_transient(tmp_path, backend):
    store = (
        SafetyStateStore()
        if backend == "memory"
        else SQLiteSafetyStateStore(tmp_path / "safety-owner.db")
    )
    store.reconcile(
        "motion-monitor",
        [
            SafetySignal(
                code="motion-monitor.near_collision",
                source="motion-monitor",
                level=SafetyLevel.P1,
            )
        ],
    )
    snapshot = store.observe_feedback(
        EnvironmentFeedback(session_id=uuid4(), safety_level=SafetyLevel.P0)
    )
    assert "motion-monitor.near_collision" in {item.code for item in snapshot.signals}
    assert snapshot.level is SafetyLevel.P1


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_environment_feedback_reconciles_its_own_transient_signals(tmp_path, backend):
    store = (
        SafetyStateStore()
        if backend == "memory"
        else SQLiteSafetyStateStore(tmp_path / "safety-environment.db")
    )
    store.observe_feedback(
        EnvironmentFeedback(
            session_id=uuid4(),
            safety_level=SafetyLevel.P2,
            signals=[
                EnvironmentSignal(
                    name="environment.obstacle",
                    source="environment",
                    safety_level=SafetyLevel.P2,
                )
            ],
        )
    )
    recovered = store.observe_feedback(
        EnvironmentFeedback(session_id=uuid4(), safety_level=SafetyLevel.P0)
    )
    assert "environment.obstacle" not in {item.code for item in recovered.signals}
    assert "environment.level" not in {item.code for item in recovered.signals}
    assert recovered.level is SafetyLevel.P0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_safety_snapshot_materializes_summary_level_without_losing_it(tmp_path, backend):
    store = (
        SafetyStateStore()
        if backend == "memory"
        else SQLiteSafetyStateStore(tmp_path / "safety-summary.db")
    )
    snapshot = reconcile_safety_snapshot(
        store,
        "camera-monitor",
        SafetyLevel.P3,
        [
            SafetySignal(
                code="camera-monitor.occlusion",
                level=SafetyLevel.P1,
                source="camera-monitor",
            )
        ],
        source="camera-monitor",
        reason="unsafe",
        synthetic_code="camera-monitor.compliance",
    )
    assert snapshot.level is SafetyLevel.P3
    assert {item.code for item in snapshot.signals} >= {
        "camera-monitor.occlusion",
        "camera-monitor.compliance",
    }


def test_loop_waits_for_current_action_and_join():
    async def scenario():
        events = []

        async def policy(ctx):
            ctx.values["count"] = ctx.previous.get("count", 0) + 1

        async def action(ctx):
            await asyncio.sleep(0.01)
            events.append(("action_done", ctx.results["policy"]["count"]))

        async def judge(ctx):
            assert events[-1] == ("action_done", ctx.results["policy"]["count"])
            events.append(("judge", ctx.results["policy"]["count"]))

        g = graph(
            "loop",
            [
                {'id': 'policy', 'module': 'policy', 'predecessors': [], 'next': [{'node': 'action'}, {'node': 'other'}]},
                {'id': 'action', 'module': 'action', 'predecessors': ['policy'], 'next': [{'node': 'judge'}]},
                {'id': 'other', 'module': 'noop', 'predecessors': ['policy'], 'next': [{'node': 'judge'}]},
                {'id': 'judge', 'module': 'judge', 'predecessors': ['action', 'other'], 'default': 'END', 'goto': [{'node': 'policy', 'when': "ctx.results['policy']['count'] < 3"}]},
            ],
        )
        result = await runtime(
            [g], dict(policy=policy, action=action, judge=judge, noop=noop)
        ).run("loop")
        assert result.success, result.error
        assert events == [
            (event, n) for n in range(1, 4) for event in ("action_done", "judge")
        ]

    asyncio.run(scenario())


def test_same_context_cannot_be_run_concurrently():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(ctx):
            entered.set()
            await release.wait()
            return {"done": True}

        g = graph("single", [{"id": "slow", "module": "slow"}])
        run = runtime([g], {"slow": slow})
        ctx = GraphContext()
        first = asyncio.create_task(run.run("single", ctx))
        await entered.wait()
        with pytest.raises(RuntimeError, match="already running"):
            await run.run("single", ctx)
        release.set()
        result = await first
        assert result.success

    asyncio.run(scenario())


def test_parallel_subgraphs_have_independent_frames_and_scoped_outputs():
    async def scenario():
        identities, stacks, positions = [], [], []
        both_entered = asyncio.Event()

        async def child_work(ctx):
            identities.append(id(ctx))
            stacks.append(ctx.graph_stack)
            before = ctx.current_node, ctx.current_graph
            if len(identities) == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), 0.5)
            positions.append((before, (ctx.current_node, ctx.current_graph)))
            return {"ok": True}

        parent = graph(
            "parent",
            [
                {'id': 'start', 'module': 'noop', 'next': [{'node': 'a'}, {'node': 'b'}]},
                {"id": "a", "graph": "child", "predecessors": ["start"]},
                {"id": "b", "graph": "child", "predecessors": ["start"]},
            ],
        )
        child = graph("child", [{'id': 'work', 'module': 'work', 'next': [{'node': 'END'}]}])
        ctx = GraphContext()
        result = await runtime([parent, child], dict(noop=noop, work=child_work)).run(
            "parent", ctx
        )
        assert result.success, result.error
        assert identities == [id(ctx), id(ctx)]
        assert stacks == [["parent", "child"], ["parent", "child"]]
        assert all(before == after == ("work", "child") for before, after in positions)
        assert len([key for key in ctx.scoped_outputs if '"work"' in key]) == 2
        assert ctx.to_dict()["graph_stack"] == []

    asyncio.run(scenario())


def test_checkpoint_resumes_unfinished_node_without_replaying_committed_node(tmp_path):
    async def scenario():
        store = SQLiteContextStore(tmp_path / "ctx.db")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def first(ctx):
            calls.append("first")
            return {"phase": "prepared"}

        async def second(ctx):
            entered.set()
            await release.wait()
            calls.append("second")

        g = graph(
            "main",
            [
                {'id': 'first', 'module': 'first', 'next': [{'node': 'second'}]},
                {"id": "second", "module": "second", "predecessors": ["first"]},
            ],
        )
        run = runtime([g], dict(first=first, second=second), context_store=store)
        session_id = uuid4()
        pending = asyncio.create_task(
            run.run("main", GraphContext(session_id=session_id))
        )
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        # New runtime represents a restarted Workflow process.
        restarted = runtime([g], dict(first=first, second=second), context_store=store)
        result = await restarted.run("main", GraphContext(session_id=session_id))
        assert result.success, result.error
        assert calls == ["first", "second"]
        assert result.context.results["first"]["phase"] == "prepared"

    asyncio.run(scenario())


def test_invalid_checkpoint_and_graph_change_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="checkpoint version"):
        GraphContext.from_dict({"checkpoint_version": 999})
    with pytest.raises(ValueError, match="legacy in-flight"):
        GraphContext.from_dict({"graph_stack": ["old"]})
    with pytest.raises(ValueError, match="TaskItem"):
        GraphContext.from_dict({"task_item": {"invalid": True}})

    async def scenario():
        g = graph("g", [{"id": "n", "module": "noop"}])
        ctx = GraphContext(graph_fingerprint="old", cursors={"g": {}}, status="running")
        with pytest.raises(ValueError, match="fingerprint"):
            await runtime([g], dict(noop=noop)).run("g", ctx)

    asyncio.run(scenario())


def test_corrupt_cursor_fails_before_executing_a_node():
    async def scenario():
        calls = []

        async def action(ctx):
            calls.append("executed")

        g = graph("g", [{"id": "a", "module": "action"}])
        run = runtime([g], {"action": action})
        ctx = GraphContext(
            graph_fingerprint=run.fingerprint,
            cursors={json.dumps(["g"]): {"pending": ["missing"]}},
        )
        result = await run.run("g", ctx)
        assert not result.success and "invalid checkpoint cursor" in result.error
        assert not calls

    asyncio.run(scenario())


def test_checkpoint_contract_and_scoped_outputs_are_bounded():
    ctx = GraphContext(scoped_outputs_limit=2)
    ctx.record_scoped_output("a", 1)
    ctx.record_scoped_output("b", 2)
    ctx.record_scoped_output("c", 3)
    assert list(ctx.scoped_outputs) == ["b", "c"]
    assert ctx.scoped_outputs_dropped == 1
    checkpoint = ctx.to_checkpoint()
    assert isinstance(checkpoint, RunCheckpoint)
    restored = GraphContext.from_checkpoint(checkpoint)
    assert restored.scoped_outputs == {"b": 2, "c": 3}
    assert restored.scoped_outputs_limit == 2


def test_graph_context_rejects_invalid_execution_contract():
    with pytest.raises(ValueError, match="history_limit"):
        GraphContext(history_limit=0)
    with pytest.raises(ValueError, match="status"):
        GraphContext(status="unknown")
    with pytest.raises(ValueError, match="step"):
        GraphContext(step=-1)
    with pytest.raises(ValueError, match="history_limit"):
        GraphContext.from_dict({"history_limit": 0})
    with pytest.raises(ValueError, match="scoped_outputs_limit"):
        GraphContext.from_dict({"scoped_outputs_limit": -1})


def test_failure_cancels_a_blocked_sibling():
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def block(ctx):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def fail(ctx):
            await entered.wait()
            raise ValueError("branch failed")

        g = graph(
            "g",
            [
                {'id': 'start', 'module': 'noop', 'next': [{'node': 'block'}, {'node': 'fail'}]},
                {"id": "block", "module": "block", "predecessors": ["start"]},
                {"id": "fail", "module": "fail", "predecessors": ["start"]},
            ],
        )
        result = await asyncio.wait_for(
            runtime([g], dict(noop=noop, block=block, fail=fail)).run("g"), 1
        )
        assert not result.success and "branch failed" in result.error
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_remote_interrupt_uses_a_free_control_channel(monkeypatch):
    from robotenon.schemas import ActionChunk

    async def scenario():
        entered, release, interrupted = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def call(client, method, params=None, **kwargs):
            async with client._lock:
                if method == "execute":
                    entered.set()
                    await release.wait()
                    chunk = params["action_chunk"]
                    return {
                        "chunk_id": chunk["chunk_id"],
                        "session_id": chunk["session_id"],
                        "status": "succeeded",
                    }
                if method == "interrupt":
                    interrupted.set()
                    release.set()
                    return {
                        "chunk_id": str(uuid4()),
                        "session_id": str(uuid4()),
                        "status": "interrupted",
                    }

        monkeypatch.setattr(JsonRpcClient, "call", call)
        client = BodyServiceClient("unused")
        execute = asyncio.create_task(
            client.execute(ActionChunk(session_id=uuid4(), actions=[]))
        )
        await entered.wait()
        await asyncio.wait_for(client.interrupt("test"), 0.5)
        assert interrupted.is_set()
        await execute

    asyncio.run(scenario())


def test_imports_do_not_load_workflow_or_create_databases(tmp_path):
    import os
    import subprocess
    import sys

    env = dict(
        os.environ,
        ROBOTENON_SAFETY_DB=str(tmp_path / "safety.db"),
        ROBOTENON_CONTEXT_DB=str(tmp_path / "context.db"),
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import robotenon.schemas, robotenon.services.ipc, sys; "
            "assert 'robotenon.workflow.runtime' not in sys.modules",
        ],
        env=env,
        check=True,
    )
    assert not list(tmp_path.glob("*.db"))


def test_graph_only_string_call_receives_graph_context():
    from robotenon.modules import NodeResult

    async def scenario():
        async def module(ctx):
            assert isinstance(ctx, GraphContext)
            ctx.action_chunk = None
            return NodeResult(output="ok", state_patch={"ready": True})

        modules = ModuleRuntime({"m": Fn(module)})
        result = await modules.call("m", {"input": 1})
        assert result.output.output == "ok"

    asyncio.run(scenario())


def test_module_runtime_preserves_graph_context_identity_for_compat_calls():
    async def scenario():
        async def module(ctx):
            ctx.values["called"] = True
            return {"same": True}

        modules = ModuleRuntime({"m": Fn(module)})
        ctx = GraphContext(values={"input": 1})
        result = await modules.call("m", context=ctx)
        assert result.ok
        assert ctx.values["called"] is True
        assert result.output == {"same": True}

    asyncio.run(scenario())


def test_startup_cancellation_rolls_back_partial_modules():
    async def scenario():
        events, started = [], asyncio.Event()

        class First:
            async def start(self):
                events.append("first.start")

            def close(self):
                events.append("first.close")

        class Second:
            async def start(self):
                started.set()
                await asyncio.Event().wait()

            def close(self):
                events.append("second.close")
                raise ValueError("cleanup failed")

        modules = ModuleRuntime({"first": First(), "second": Second()})
        task = asyncio.create_task(modules.start())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == ["first.start", "second.close", "first.close"]
        assert not modules.started

    asyncio.run(scenario())


def test_body_enforces_p2_gate_at_execution_boundary():
    from robotenon.runtime.safety import SafetyGate
    from robotenon.schemas import Action, ActionChunk, ActionType
    from robotenon.services import InProcessBodyService

    async def scenario():
        calls = []
        safety = SafetyStateStore()
        safety.raise_level(SafetyLevel.P2, code="limited")
        body = InProcessBodyService(
            calls.append,
            safety_state=safety,
            safety_gate=SafetyGate(limited_action_types={"wait"}),
        )
        session_id = uuid4()
        try:
            result = await body.execute(
                ActionChunk(
                    session_id=session_id,
                    actions=[
                        Action(
                            session_id=session_id, action_type=ActionType.JOINT_POSITION
                        )
                    ],
                )
            )
            assert result.status.value == "rejected"
            assert not calls
        finally:
            await body.close()

    asyncio.run(scenario())


def test_rpc_total_deadline_includes_lock_wait():
    async def scenario():
        client = JsonRpcClient("unused", timeout=0.01)
        await client._lock.acquire()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(client.call("health"), 0.2)
        finally:
            client._lock.release()
        assert client._writer is None

    asyncio.run(scenario())


def test_ipc_protocol_has_version_and_message_budget():
    from robotenon.services.ipc import DEFAULT_MAX_MESSAGE_BYTES, RPC_VERSION

    assert RPC_VERSION == 1
    assert DEFAULT_MAX_MESSAGE_BYTES > 0
    with pytest.raises(ValueError):
        JsonRpcClient("unused", max_message_bytes=0)
    with pytest.raises(ValueError):
        JsonRpcServer("/tmp/robotenon-invalid.sock", {}, max_message_bytes=0)

def test_parallel_explicit_patches_are_node_private():
    from robotenon.modules import NodeResult

    async def scenario():
        async def write(ctx):
            return NodeResult(state_patch={"target": ctx.current_node})

        g = graph(
            "g",
            [
                {'id': 'start', 'module': 'noop', 'next': [{'node': 'a'}, {'node': 'b'}]},
                {"id": "a", "module": "write", "predecessors": ["start"]},
                {"id": "b", "module": "write", "predecessors": ["start"]},
            ],
        )
        result = await runtime([g], dict(noop=noop, write=write)).run("g")
        assert result.success, result.error
        assert result.context.results["a"]["target"] == "a"
        assert result.context.results["b"]["target"] == "b"

    asyncio.run(scenario())


def test_service_context_omits_execution_state():
    from robotenon.contracts.service_context import ServiceContext

    ctx = GraphContext(
        values={"goal": "pick"},
        history=[{"node": "a"}],
        cursors={"a": {}},
        context_revision=7,
    )
    request = ServiceContext.from_context(ctx).model_dump()
    assert request["values"] == {"goal": "pick"} and request["context_revision"] == 7
    assert not ({"history", "cursors", "outputs", "graph_stack"} & request.keys())


def test_graph_context_history_is_a_bounded_diagnostic_window():
    ctx = GraphContext(history_limit=2)
    ctx.append_history({"event": "one"})
    ctx.append_history({"event": "two"})
    ctx.append_history({"event": "three"})
    assert ctx.history == [{"event": "two"}, {"event": "three"}]
    assert ctx.history_dropped == 1
    restored = GraphContext.from_dict(ctx.to_dict())
    assert restored.history == ctx.history
    assert restored.history_dropped == 1


def test_checkpoint_serialization_rejects_unknown_objects_and_non_finite_numbers(tmp_path):
    """Durable context must fail closed instead of silently stringifying state."""

    ctx = GraphContext(values={"non_finite": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        ctx.to_json()

    class UnsupportedContext:
        session_id = "strict"

        def to_dict(self):
            return {"opaque": object()}

    store = SQLiteContextStore(tmp_path / "strict-context.db")
    with pytest.raises(TypeError, match="unsupported checkpoint value"):
        store.save(UnsupportedContext())


def test_explicit_output_does_not_implicitly_write_business_state():
    from robotenon.modules import NodeResult

    async def scenario():
        async def module(ctx):
            return NodeResult(output={"internal": 1}, state_patch={"ready": True})

        g = graph("g", [{"id": "n", "module": "m"}])
        result = await runtime([g], {"m": module}).run("g")
        assert result.success
        assert result.context.results["n"] == {"ready": True}
        assert result.context.outputs["n"] == {"internal": 1}

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_real_body_rpc_interrupts_long_execute(cancel):
    """Local UDS integration: stop arrives while execute is still in flight."""
    from pathlib import Path
    from robotenon.services import InProcessBodyService, BodyServiceServer
    from robotenon.schemas import Action, ActionChunk, ActionType

    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()

        async def driver(action):
            started.set()
            await asyncio.Event().wait()

        body = InProcessBodyService(
            driver,
            stop_executor=lambda reason: stopped.set(),
            safety_state=SafetyStateStore(),
        )
        server = BodyServiceServer(Path("/tmp") / f"rs-{uuid4().hex}.sock", body)
        try:
            await server.start()
        except PermissionError:
            pytest.skip("sandbox does not permit Unix Domain Socket binding")
        client = BodyServiceClient(server.socket_path)
        session_id = uuid4()
        task = asyncio.create_task(
            client.execute(
                ActionChunk(
                    session_id=session_id,
                    max_duration_ms=5000,
                    actions=[
                        Action(
                            session_id=session_id,
                            action_type=ActionType.WAIT,
                            timeout_ms=5000,
                        )
                    ],
                )
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert (await asyncio.wait_for(client.health(), 0.5)).ready
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await asyncio.wait_for(client.interrupt("regression"), 0.5)
                result = await task
                assert result.interrupted
            assert stopped.is_set()
            # A fresh request after cancellation must not read an old response.
            assert (await client.health()).ready
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.close_connection()
            await body.close()
            await server.close()

    asyncio.run(scenario())
