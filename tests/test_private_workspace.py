"""Ownership, visibility, lifecycle and durable publication contracts."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from robotenon.contracts.context import GraphContext
from robotenon.contracts.module import NodeResult
from robotenon.core.context import SQLiteContextStore
from robotenon.schemas import Action, ActionType
from test_graph_parallel_routing import node, runtime


def test_shared_context_with_private_same_key_and_recursive_readonly_results():
    async def scenario():
        ctx = GraphContext(values={"task": {"items": [1]}})
        identities, drafts, original = [], [], {"items": []}
        async def work(received):
            identities.append(id(received))
            assert received.values == {}
            with pytest.raises(TypeError):
                received.inputs["task"]["new"] = True
            received.values["same"] = received.current_node
            received.values["nested"] = original
            received.values["nested"]["items"].append(received.current_node)
            drafts.append(received.values["nested"]["items"])
            await asyncio.sleep(0)
            return NodeResult()
        result = await runtime([node("a"), node("b")], work, entry=["a", "b"]).run("main", ctx)
        assert result.success, result.error
        assert identities == [id(ctx), id(ctx)]
        assert original == {"items": []}
        assert ctx.results["a"]["same"] == "a"
        assert ctx.results["b"]["same"] == "b"
        assert ctx.results["a"]["nested"]["items"] == ("a",)
        with pytest.raises(TypeError):
            ctx.results["a"]["same"] = "corrupt"
        with pytest.raises(AttributeError):
            ctx.results["a"]["nested"]["items"].append("corrupt")
        for draft in drafts:
            with pytest.raises(RuntimeError, match="closed"):
                draft.append("late")
        with pytest.raises(RuntimeError, match="node-private"):
            ctx.values["outside"] = 1
        restored = GraphContext.from_dict(ctx.to_dict())
        assert restored.results == ctx.results
    asyncio.run(scenario())


def test_wave_read_snapshot_does_not_observe_early_sibling_commit():
    async def scenario():
        async def work(ctx):
            if ctx.current_node == "slow":
                before = ctx.results
                await asyncio.sleep(0.03)
                assert "fast" not in ctx.results
                assert ctx.results == before
            if ctx.current_node == "join":
                assert ctx.results["fast"]["ready"] is True
                assert ctx.results["slow"]["ready"] is True
            return {"ready": True}
        result = await runtime([
            node('start', next=[{'node': 'fast'}, {'node': 'slow'}]),
            node('fast', predecessors=['start'], next=[{'node': 'join'}]),
            node('slow', predecessors=['start'], next=[{'node': 'join'}]),
            node("join", predecessors=["fast", "slow"]),
        ], work).run("main")
        assert result.success, result.error
    asyncio.run(scenario())


def test_failed_loop_keeps_last_successful_version_and_discards_draft():
    async def scenario():
        drafts = []
        async def work(ctx):
            ctx.values["count"] = ctx.previous.get("count", 0) + 1
            drafts.append(ctx.values)
            if ctx.values["count"] == 2:
                raise RuntimeError("failed second iteration")
        result = await runtime([node('start', loop=True)], work).run("main")
        assert not result.success
        assert result.context.results["start"]["count"] == 1
        assert result.context.result_versions["start"] == 1
        for draft in drafts:
            with pytest.raises(RuntimeError, match="closed"):
                draft["count"] = 3
    asyncio.run(scenario())


def test_late_child_task_cannot_write_after_node_returns():
    async def scenario():
        gate, children = asyncio.Event(), []
        async def work(ctx):
            ctx.values["items"] = []
            nested = ctx.values["items"]
            async def late():
                await gate.wait()
                with pytest.raises(RuntimeError, match="closed"):
                    nested.append("late")
                with pytest.raises(RuntimeError, match="closed"):
                    ctx.values["new"] = 1
            children.append(asyncio.create_task(late()))
        result = await runtime([node("start")], work).run("main")
        gate.set()
        await asyncio.gather(*children)
        assert result.success, result.error
        assert result.context.results["start"]["items"] == ()
    asyncio.run(scenario())


def test_to_thread_inherits_workspace_but_raw_thread_is_rejected():
    async def scenario():
        async def work(ctx):
            draft = ctx.values
            await asyncio.to_thread(lambda: draft.__setitem__("thread", 1))
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: draft.__setitem__("raw", 2))
                with pytest.raises(RuntimeError, match="execution context"):
                    future.result()
        result = await runtime([node("start")], work).run("main")
        assert result.success, result.error
        assert result.context.results["start"]["thread"] == 1
        assert "raw" not in result.context.results["start"]
    asyncio.run(scenario())


def test_cancellation_discards_private_values_and_typed_field_changes():
    async def scenario():
        ctx, ready = GraphContext(session_id=uuid4()), asyncio.Event()
        drafts = []
        async def work(ctx):
            ctx.values["partial"] = True
            ctx.action = Action(session_id=ctx.session_id, action_type=ActionType.WAIT)
            drafts.append(ctx.values)
            ready.set()
            await asyncio.Event().wait()
        runner = runtime([node("start")], work)
        task = asyncio.create_task(runner.run("main", ctx))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ctx.action is None
        assert "start" not in ctx.results
        assert next(iter(ctx.cursors.values()))["done"] == []
        with pytest.raises(RuntimeError, match="closed"):
            drafts[0]["partial"] = False
    asyncio.run(scenario())


def test_checkpoint_failure_does_not_publish_and_retry_reexecutes(tmp_path):
    class FailingStore:
        def __init__(self):
            self.store = SQLiteContextStore(tmp_path / "ctx.db")
            self.fail = True
        def load(self, session_id):
            return self.store.load(session_id)
        def save(self, ctx, *, expected_revision=None):
            if self.fail and "start" in ctx.results:
                self.fail = False
                raise OSError("storage unavailable")
            return self.store.save(ctx, expected_revision=expected_revision)
    async def scenario():
        calls = []
        async def work(ctx):
            calls.append(1)
            ctx.values["committed"] = True
        store = FailingStore()
        runner = runtime([node("start")], work)
        runner.context_store = store
        ctx = GraphContext(session_id=uuid4())
        first = await runner.run("main", ctx)
        assert not first.success and "storage unavailable" in first.error
        assert "start" not in ctx.results
        saved = GraphContext.from_dict(store.load(ctx.session_id))
        assert "start" not in saved.results
        assert next(iter(saved.cursors.values()))["done"] == []
        second = await runner.run("main", saved)
        assert second.success, second.error
        assert calls == [1, 1]
        assert saved.results["start"]["committed"] is True
        assert GraphContext.from_dict(store.load(ctx.session_id)).results == saved.results
    asyncio.run(scenario())


def test_typed_field_conflict_rejected_in_parallel():
    async def scenario():
        async def work(ctx):
            ctx.action = Action(session_id=ctx.session_id, action_type=ActionType.WAIT)
            return NodeResult()
        result = await runtime([node("a"), node("b")], work, entry=["a", "b"]).run("main", GraphContext(session_id=uuid4()))
        assert not result.success
        assert "@field:action" in result.error
    asyncio.run(scenario())


def test_framework_state_is_not_writable_by_nodes():
    async def scenario():
        async def work(ctx):
            with pytest.raises(AttributeError):
                ctx.values = {}
            with pytest.raises(AttributeError):
                ctx.status = "completed"
            with pytest.raises(TypeError):
                ctx.node_records["fake"] = {}
            with pytest.raises(TypeError):
                ctx.cursors["fake"] = {}
            with pytest.raises(AttributeError):
                ctx.history.append({})
            return NodeResult()
        result = await runtime([node("start")], work).run("main")
        assert result.success, result.error
    asyncio.run(scenario())


def test_context_lease_prevents_two_runtimes_sharing_a_live_context():
    async def scenario():
        ctx, ready, stop = GraphContext(), asyncio.Event(), asyncio.Event()
        async def work(ctx):
            ready.set()
            await stop.wait()
        one = runtime([node("start")], work)
        two = runtime([node("start")], work)
        task = asyncio.create_task(one.run("main", ctx))
        await ready.wait()
        with pytest.raises(RuntimeError, match="already running"):
            await two.run("main", ctx)
        stop.set()
        assert (await task).success
    asyncio.run(scenario())


def test_resume_keeps_original_wave_read_snapshot(tmp_path):
    async def scenario():
        seen, fail = [], True
        async def work(ctx):
            seen.append(ctx.current_node)
            if ctx.current_node == "slow":
                await asyncio.sleep(0.01)
                assert "fast" not in ctx.results
                if fail:
                    raise RuntimeError("interrupt slow branch")
            return {"ready": True}
        runner = runtime([node("fast"), node("slow")], work, entry=["fast", "slow"])
        runner.context_store = SQLiteContextStore(tmp_path / "wave.db")
        ctx = GraphContext(session_id=uuid4())
        first = await runner.run("main", ctx)
        assert not first.success
        assert ctx.results["fast"]["ready"] is True
        restored = GraphContext.from_dict(runner.context_store.load(ctx.session_id))
        fail = False
        second = await runner.run("main", restored)
        assert second.success, second.error
        assert seen.count("fast") == 1 and seen.count("slow") == 2
    asyncio.run(scenario())


def test_cancellation_during_storage_finishes_commit_before_propagating(tmp_path):
    import threading
    class PausingStore:
        def __init__(self):
            self.store = SQLiteContextStore(tmp_path / "cancel.db")
            self.started, self.release = threading.Event(), threading.Event()
        def load(self, session_id):
            return self.store.load(session_id)
        def save(self, ctx, *, expected_revision=None):
            if "start" in ctx.results and not self.started.is_set():
                self.started.set()
                assert self.release.wait(5)
            return self.store.save(ctx, expected_revision=expected_revision)
    async def scenario():
        calls = []
        async def work(ctx):
            calls.append(1)
            return {"ok": True}
        store = PausingStore()
        runner = runtime([node("start")], work)
        runner.context_store = store
        ctx = GraphContext(session_id=uuid4())
        task = asyncio.create_task(runner.run("main", ctx))
        assert await asyncio.to_thread(store.started.wait, 5)
        task.cancel()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        restored = GraphContext.from_dict(store.load(ctx.session_id))
        assert restored.results["start"]["ok"] is True
        result = await runner.run("main", restored)
        assert result.success, result.error
        assert calls == [1]
    asyncio.run(scenario())


def test_builtin_planner_and_verifier_exchange_published_memory():
    from types import SimpleNamespace

    from robotenon.infra.params import Params
    from robotenon.modules.plan import PlanModule
    from robotenon.modules.verify import VerifyModule
    from robotenon.schemas import EnvironmentFeedback

    class Context(GraphContext):
        async def call_tool(self, name, arguments=None):
            if self.current_node == "plan":
                assert '"old"' in arguments["prompt"]
                assert '"lesson"' in arguments["prompt"]
                output = {"decision": "act", "subgoal": "task", "completed": ["old"], "remaining": ["task"]}
            else:
                assert "Current subtask: task" in arguments["prompt"]
                output = {"subtask_success": True, "physical_belief_update": {"closed": True}}
            return SimpleNamespace(ok=True, output=output, metadata={})
    async def scenario():
        ctx = Context(
            values={"stm": {"completed": ["old"], "remaining": ["task"]}, "ltm_records": [{"lesson": "example"}]},
            feedback=EnvironmentFeedback(session_id=uuid4(), sequence_no=0),
            params=Params({"model": {"hlp_tool": "model"}, "task": {"task_id": "task"}}),
        )
        async def work(ctx):
            return await (PlanModule() if ctx.current_node == "plan" else VerifyModule()).run(ctx)
        result = await runtime([node('plan', next=[{'node': 'verify'}]), node("verify", predecessors=["plan"])], work, entry="plan").run("main", ctx)
        assert result.success, result.error
        assert ctx.results["plan"]["stm"]["completed"] == ("old",)
        assert ctx.results["verify"]["stm"]["completed"] == ("old", "task")
        assert ctx.results["verify"]["stm"]["remaining"] == ()
        assert ctx.inputs["stm"]["remaining"] == ("task",)
    asyncio.run(scenario())
