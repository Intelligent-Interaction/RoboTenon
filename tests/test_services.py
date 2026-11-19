import asyncio
import time
from pathlib import Path

import pytest

from robotenon.bodies.fake import FakeEnvironmentActionDriver
from robotenon.envs.base.fake import FakeEnvironmentAdapter
from robotenon.envs import bind_environment, environment
from robotenon.runtime.safety import SafetyStateStore
from robotenon.runtime.safety import SQLiteSafetyStateStore
from robotenon.runtime.config import ModuleConfig, ModulesConfig
from robotenon.runtime.factory import build_module_runtime
from robotenon.schemas import Action, ActionChunk, ActionType, SafetyLevel, SessionSpec
from robotenon.services import (
    EnvironmentServiceClient,
    EnvironmentServiceServer,
    InProcessBodyService,
    SafetyMonitorService,
    ServiceSupervisor,
)
from robotenon.workflow import GraphContext
from robotenon.workflow import GraphDefinition, GraphRuntime


def test_environment_service_observe_is_read_only_and_ipc_round_trips(tmp_path: Path):
    async def scenario():
        adapter = FakeEnvironmentAdapter(max_steps=3)
        session = await adapter.open(SessionSpec(task_id="ipc", environment_id="fake"))
        service = session
        driver = FakeEnvironmentActionDriver(session)
        reset = await service.reset(GraphContext(task_id="ipc"))
        before = await service.observe()
        stepped = await driver.execute(Action(session_id=reset.session_id, action_type=ActionType.WAIT))
        after = await service.observe()
        assert before.sequence_no == reset.sequence_no
        assert after.sequence_no == stepped.sequence_no

        # macOS limits AF_UNIX paths to roughly 104 bytes; pytest's temporary
        # directory can exceed that once nested under the workspace.
        from uuid import uuid4

        socket = Path("/tmp") / f"robotenon-{uuid4().hex}.sock"
        server = EnvironmentServiceServer(socket, service)
        try:
            await server.start()
        except PermissionError:
            # Some managed macOS sandboxes disallow binding AF_UNIX sockets;
            # the same transport is exercised in normal Linux/macOS hosts.
            await service.close()
            pytest.skip("sandbox does not permit Unix Domain Socket binding")
        client = EnvironmentServiceClient(socket)
        try:
            remote = await client.observe()
            assert remote.session_id == stepped.session_id
            assert remote.sequence_no == stepped.sequence_no
        finally:
            await client.close_connection()
            await server.close()
            await service.close()

    asyncio.run(scenario())


def test_environment_observe_returns_last_snapshot_during_long_body_action():
    async def scenario():
        from robotenon.envs.base.fake import FakeEnvironmentSession
        from robotenon.schemas import EnvironmentFeedback

        started = asyncio.Event()
        release = asyncio.Event()
        session = FakeEnvironmentSession(
            SessionSpec(task_id="observe-during-step", environment_id="fake"),
            max_steps=2,
        )
        driver = FakeEnvironmentActionDriver(session)
        original_execute = driver.execute

        async def slow_execute(action):
            started.set()
            await release.wait()
            return await original_execute(action)

        driver.execute = slow_execute
        service = session
        reset = await service.reset(session.spec)
        pending = asyncio.create_task(
            driver.execute(Action(session_id=reset.session_id, action_type=ActionType.WAIT))
        )
        await started.wait()
        observed = await asyncio.wait_for(service.observe(), timeout=0.1)
        assert isinstance(observed, EnvironmentFeedback)
        assert observed.sequence_no == reset.sequence_no
        release.set()
        stepped = await pending
        assert stepped.sequence_no == reset.sequence_no + 1
        await service.close()

    asyncio.run(scenario())


def test_monitor_publishes_compliance_to_global_safety_store():
    async def scenario():
        adapter = FakeEnvironmentAdapter(max_steps=2)
        session = await adapter.open(SessionSpec(task_id="monitor", environment_id="fake"))
        env = session
        feedback = await env.reset(GraphContext(task_id="monitor"))
        store = SafetyStateStore()

        async def checker(observation, context):
            assert observation.session_id == feedback.session_id
            assert context is not None
            return {"compliant": False, "safety_level": "P3", "reason": "workspace blocked"}

        monitor = SafetyMonitorService(env, checker=checker, safety_state=store, poll_hz=1000)
        report = await monitor.check(feedback, context=GraphContext(feedback=feedback))
        assert report.compliant is False
        assert report.safety_level is SafetyLevel.P3
        assert store.snapshot().level is SafetyLevel.P3
        await monitor.close()
        await env.close()

    asyncio.run(scenario())


def test_body_watchdog_interrupts_chunk_and_runs_custom_safe_action():
    async def scenario():
        store = SafetyStateStore()
        calls = []
        stops = []
        started = asyncio.Event()
        from uuid import uuid4

        body_session = uuid4()

        async def executor(action):
            calls.append(action.action_type)
            if len(calls) == 1:
                started.set()
                await asyncio.sleep(10)

        async def stop(reason):
            stops.append(reason)

        def safe_action(snapshot):
            return Action(session_id=body_session, action_type=ActionType.WAIT)

        body = InProcessBodyService(
            executor,
            safety_state=store,
            poll_hz=500,
            safe_action_factory=safe_action,
            stop_executor=stop,
        )
        chunk = ActionChunk(
            session_id=body_session,
            actions=[Action(session_id=body_session, action_type=ActionType.CUSTOM)],
        )
        task = asyncio.create_task(body.execute(chunk))
        await asyncio.wait_for(started.wait(), timeout=1)
        store.raise_level(SafetyLevel.P3, code="test.blocked", source="test")
        result = await asyncio.wait_for(task, timeout=1)
        assert result.interrupted is True
        assert result.status.value == "interrupted"
        assert stops
        assert len(calls) == 2  # interrupted action plus the safe WAIT action
        await body.close()

    asyncio.run(scenario())


def test_sqlite_safety_state_is_visible_to_independent_process_clients(tmp_path: Path):
    path = tmp_path / "safety.sqlite3"
    first = SQLiteSafetyStateStore(path)
    second = SQLiteSafetyStateStore(path)
    first.raise_level(SafetyLevel.P3, code="monitor.workspace", source="monitor")
    assert second.snapshot().level is SafetyLevel.P3
    with pytest.raises(PermissionError):
        second.recover()
    assert second.recover(approved=True).level is SafetyLevel.P0


def test_sqlite_safety_incident_identity_is_shared_and_clears_after_recovery(tmp_path: Path):
    path = tmp_path / "incident.sqlite3"
    first = SQLiteSafetyStateStore(path)
    second = SQLiteSafetyStateStore(path)
    first.raise_level(SafetyLevel.P3, code="monitor.blocked", source="monitor")
    first_snapshot = first.snapshot()
    second_snapshot = second.snapshot()
    assert first_snapshot.incident_id is not None
    assert second_snapshot.incident_id == first_snapshot.incident_id
    recovered = second.recover(approved=True)
    assert recovered.level is SafetyLevel.P0
    assert first.snapshot().incident_id is None


def test_safety_heartbeat_fails_closed_and_renews():
    async def scenario():
        store = SafetyStateStore()
        store.heartbeat("monitor", ttl_s=0.02)
        assert store.snapshot().level is SafetyLevel.P0
        await asyncio.sleep(0.04)
        expired = store.snapshot()
        assert expired.level is SafetyLevel.P3
        assert any(signal.code == "monitor.heartbeat" for signal in expired.signals)
        assert expired.incident_id is not None
        store.heartbeat("monitor", ttl_s=0.2)
        assert store.snapshot().level is SafetyLevel.P0

    asyncio.run(scenario())


def test_body_enforces_action_deadline_and_emits_acknowledgement():
    async def scenario():
        store = SafetyStateStore()
        stopped = []

        async def executor(action):
            await asyncio.sleep(0.05)

        async def stop(reason):
            stopped.append(reason)

        body = InProcessBodyService(executor, safety_state=store, stop_executor=stop)
        from uuid import uuid4

        session_id = uuid4()
        action = Action(session_id=session_id, action_type=ActionType.WAIT, timeout_ms=5)
        result = await body.execute(
            ActionChunk(session_id=session_id, actions=[action], max_duration_ms=100)
        )
        assert result.status.value == "failed"
        assert result.deadline_exceeded is True
        assert result.action_results[0].status == "deadline_exceeded"
        assert stopped
        await body.close()

    asyncio.run(scenario())


def test_body_honours_absolute_deadline_before_relative_timeout():
    async def scenario():
        store = SafetyStateStore()
        calls = []

        async def executor(action):
            calls.append(action)
            await asyncio.sleep(0.2)

        from time import time_ns
        from uuid import uuid4

        session_id = uuid4()
        body = InProcessBodyService(executor, safety_state=store, poll_hz=1000)
        action = Action(
            session_id=session_id,
            action_type=ActionType.WAIT,
            timeout_ms=500,
            deadline_ns=time_ns() + 10_000_000,
        )
        result = await body.execute(ActionChunk(session_id=session_id, actions=[action], max_duration_ms=500))
        assert result.deadline_exceeded is True
        assert result.action_results[0].status == "deadline_exceeded"
        assert calls
        assert store.snapshot().level is SafetyLevel.P3
        await body.close()

    asyncio.run(scenario())


def test_body_stop_callback_failure_escalates_to_latched_p4():
    async def scenario():
        store = SafetyStateStore()

        async def executor(action):
            await asyncio.sleep(0.05)

        async def stop(reason):
            raise RuntimeError("hardware stop unavailable")

        from uuid import uuid4

        session_id = uuid4()
        body = InProcessBodyService(
            executor,
            safety_state=store,
            stop_executor=stop,
            stop_timeout_s=0.1,
        )
        result = await body.execute(ActionChunk(session_id=session_id, actions=[
            Action(session_id=session_id, action_type=ActionType.WAIT, timeout_ms=1)
        ]))
        assert result.deadline_exceeded is True
        assert store.snapshot().level is SafetyLevel.P4
        await body.close()

    asyncio.run(scenario())


def test_synchronous_stop_callback_is_bounded_and_escalates_p4():
    async def scenario():
        store = SafetyStateStore()

        def hanging_stop(_reason):
            time.sleep(1.0)

        body = InProcessBodyService(
            lambda _action: None,
            safety_state=store,
            stop_executor=hanging_stop,
            stop_timeout_s=0.01,
        )
        started = time.perf_counter()
        result = await body.interrupt("bounded_stop")
        elapsed = time.perf_counter() - started
        assert elapsed < 0.2
        assert result.interrupted
        assert store.snapshot().level is SafetyLevel.P4
        await body.close()

    asyncio.run(scenario())


def test_body_idempotency_is_session_scoped_and_interrupt_returns_ack():
    async def scenario():
        store = SafetyStateStore()
        calls = []
        started = asyncio.Event()

        async def executor(action):
            calls.append(action)
            if action.payload.get("long"):
                started.set()
                await asyncio.sleep(10)

        from uuid import uuid4

        session_one = uuid4()
        session_two = uuid4()
        body = InProcessBodyService(executor, safety_state=store)
        first = Action(session_id=session_one, action_type=ActionType.WAIT, idempotency_key="same")
        successful = Action(session_id=session_one, action_type=ActionType.WAIT, idempotency_key="same")
        second_session = Action(session_id=session_two, action_type=ActionType.WAIT, idempotency_key="same")
        # First action completes and the same key is acknowledged from cache.
        await body.execute(ActionChunk(session_id=session_one, actions=[successful]))
        cached = await body.execute(ActionChunk(session_id=session_one, actions=[first]))
        assert cached.executed_actions == 0
        assert len(calls) == 1
        # The same key in a new session must execute independently.
        await body.execute(ActionChunk(session_id=session_two, actions=[second_session]))
        assert len(calls) == 2
        # An active action returns an explicit interrupted acknowledgement.
        active = asyncio.create_task(body.execute(ActionChunk(session_id=session_one, actions=[
            Action(session_id=session_one, action_type=ActionType.CUSTOM, idempotency_key="long", payload={"long": True})
        ])))
        await asyncio.wait_for(started.wait(), timeout=1)
        interrupted = await body.interrupt("test_interrupt")
        assert interrupted.action_results and interrupted.action_results[0].status == "interrupted"
        await active
        await body.close()

    asyncio.run(scenario())


def test_body_idempotency_ledger_survives_service_restart(tmp_path: Path):
    async def scenario():
        from uuid import uuid4

        session_id = uuid4()
        calls = []

        async def executor(action):
            calls.append(action)

        action = Action(session_id=session_id, action_type=ActionType.WAIT, idempotency_key="durable")
        ledger_path = tmp_path / "actions.sqlite3"
        first = InProcessBodyService(executor, idempotency_db=ledger_path)
        first_result = await first.execute(ActionChunk(session_id=session_id, actions=[action]))
        assert first_result.action_results[0].status == "succeeded"
        await first.close()

        second = InProcessBodyService(executor, idempotency_db=ledger_path)
        retried = await second.execute(ActionChunk(session_id=session_id, actions=[action]))
        assert retried.executed_actions == 0
        assert retried.action_results[0].status == "succeeded"
        assert len(calls) == 1
        await second.close()

    asyncio.run(scenario())


def test_service_supervisor_fails_closed_when_startup_fails():
    async def scenario():
        store = SafetyStateStore()

        class BrokenService:
            async def start(self):
                raise RuntimeError("driver unavailable")

            async def health(self):
                return {"ready": False, "degraded": True, "message": "not started"}

            async def close(self):
                return None

        supervisor = ServiceSupervisor({"body": BrokenService()}, safety_state=store, poll_hz=100)
        with pytest.raises(RuntimeError, match="startup failed"):
            await supervisor.start()
        assert supervisor.statuses["body"].ready is False
        assert store.snapshot().level is SafetyLevel.P3
        await supervisor.close()

    asyncio.run(scenario())


def test_service_supervisor_health_timeout_raises_fail_safe():
    async def scenario():
        store = SafetyStateStore()

        class HangingService:
            async def start(self):
                return None

            async def health(self):
                await asyncio.sleep(10)

            async def close(self):
                return None

        supervisor = ServiceSupervisor(
            {"env": HangingService()},
            safety_state=store,
            poll_hz=100,
            health_timeout_s=0.01,
        )
        with pytest.raises(RuntimeError, match="startup failed"):
            # start performs one bounded probe; a timeout cannot leave the
            # supervisor silently alive with an unbounded health call.
            await supervisor.start()
        assert store.snapshot().level is SafetyLevel.P3
        await supervisor.close()

    asyncio.run(scenario())


def test_graph_service_nodes_keep_one_ctx_and_service_channel():
    async def scenario():
        adapter = FakeEnvironmentAdapter(max_steps=1)
        session = await adapter.open(SessionSpec(task_id="graph", environment_id="fake"))
        env = session
        store = SafetyStateStore()
        driver = FakeEnvironmentActionDriver(session)
        body = InProcessBodyService(driver.execute, safety_state=store)
        monitor = SafetyMonitorService(session, safety_state=store, poll_hz=100)
        graph = GraphDefinition.model_validate(
            {
                "id": "service-loop",
                "entry": "reset",
                "backend": "fallback",
                "nodes": [
                    {'id': 'reset', 'type': 'system', 'operation': 'env.reset', 'next': [{'node': 'policy'}]},
                    {'id': 'policy', 'type': 'module', 'module': 'wait-agent', 'predecessors': ['reset'], 'next': [{'node': 'body'}]},
                    {'id': 'body', 'type': 'system', 'operation': 'body.execute', 'predecessors': ['policy'], 'next': [{'node': 'observe'}]},
                    {'id': 'observe', 'type': 'system', 'operation': 'env.observe', 'predecessors': ['body'], 'next': [{'node': 'monitor'}]},
                    {'id': 'monitor', 'type': 'system', 'operation': 'monitor.check', 'predecessors': ['observe'], 'next': [{'node': 'judge'}]},
                    {'id': 'judge', 'type': 'module', 'module': 'terminal-evaluator', 'predecessors': ['monitor'], 'next': [{'node': 'END'}]},
                ],
            }
        )
        modules = build_module_runtime(
            ModulesConfig(
                modules=[
                    ModuleConfig(id="wait-agent", name="wait-agent"),
                    ModuleConfig(id="terminal-evaluator", name="terminal-evaluator"),
                ]
            )
        )
        ctx = GraphContext(task_id="graph")
        runtime = GraphRuntime({graph.id: graph}, modules, backend="fallback", safety_state=store)
        previous = bind_environment(env)
        try:
            result = await runtime.run("service-loop", ctx=ctx, body=body, monitor=monitor)
            assert result.success is True
            assert result.context is ctx
            assert ctx.body_result is not None and ctx.body_result.executed_actions == 1
            assert ctx.monitor_report is not None
            assert ctx.feedback is not None and ctx.feedback.terminal is True
        finally:
            environment.restore(previous)
            await body.close()
            await monitor.close()
            await env.close()
            await modules.close()

    asyncio.run(scenario())


def test_body_reports_chunk_deadline_instead_of_longer_action_timeout():
    from uuid import uuid4

    async def scenario():
        safety = SafetyStateStore()

        async def execute(action):
            await asyncio.sleep(1)

        body = InProcessBodyService(execute, safety_state=safety)
        session_id = uuid4()
        chunk = ActionChunk(session_id=session_id, max_duration_ms=25, actions=[
            Action(session_id=session_id, action_type=ActionType.WAIT, timeout_ms=120000)
        ])
        try:
            result = await body.execute(chunk)
            assert result.deadline_exceeded
            assert 'chunk deadline exceeded after 25 ms' in result.error
            assert any('action_chunk_deadline' in signal.code for signal in safety.snapshot().signals)
        finally:
            await body.close()

    asyncio.run(scenario())
