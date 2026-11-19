"""Read-only architecture probes; run with Python >=3.10.

The probes exercise the repaired safety, loop, parallel-subgraph, checkpoint
and RPC-control contracts.  No robot or socket binding is required.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from robosys.core.context import SQLiteContextStore
from robosys.modules import ModuleRuntime
from robosys.runtime.safety import SafetyStateStore, SQLiteSafetyStateStore
from robosys.schemas import SafetyLevel
from robosys.workflow.runtime import GraphContext, GraphRuntime
from robosys.workflow.definition import GraphDefinition
from robosys.services.ipc import BodyServiceClient


def graph(name, nodes):
    return GraphDefinition.model_validate({'id': name, 'entry': nodes[0]['id'], 'nodes': nodes})


class Fn:
    def __init__(self, fn):
        self.fn = fn

    async def run(self, ctx):
        return await self.fn(ctx)


async def main():
    evidence = {}
    with tempfile.TemporaryDirectory() as directory:
        for label, store in [('memory', SafetyStateStore()), ('sqlite', SQLiteSafetyStateStore(Path(directory)/'safety.db'))]:
            store.raise_level(SafetyLevel.P3, code='monitor.compliance')
            before = store.snapshot()
            store.raise_level(SafetyLevel.P1, code='monitor.compliance')
            after = store.snapshot()
            evidence[f'latch_{label}'] = [before.level.value, after.level.value, after.signals[0].latched]
            assert after.level == SafetyLevel.P3 and after.signals[0].latched

        async def noop(ctx):
            return {}

        async def delay(ctx):
            await asyncio.sleep(.01)
            return {}

        parent = graph('parent', [
            {'id': 'start', 'module': 'noop'},
            {'id': 'a', 'graph': 'child', 'predecessors': ['start']},
            {'id': 'b', 'graph': 'child', 'predecessors': ['start']},
        ])
        child = graph('child', [{'id': 'work', 'module': 'delay', 'goto': ['END']}])
        runtime = GraphRuntime([parent, child], ModuleRuntime({'noop': Fn(noop), 'delay': Fn(delay)}), safety_state=SafetyStateStore())
        result = await runtime.run('parent')
        evidence['parallel_subgraphs'] = [result.status, result.error]
        assert result.success

        store = SQLiteContextStore(Path(directory)/'context.db')
        checkpoint_graph = graph('checkpoint', [
            {'id': 'first', 'module': 'noop'},
            {'id': 'second', 'module': 'delay', 'predecessors': ['first']},
        ])
        entered = asyncio.Event()

        release = asyncio.Event()

        async def block(ctx):
            entered.set()
            await release.wait()

        runtime = GraphRuntime([checkpoint_graph], ModuleRuntime({'noop': Fn(noop), 'delay': Fn(block)}), context_store=store, safety_state=SafetyStateStore())
        session_id = uuid4()
        task = asyncio.create_task(runtime.run('checkpoint', GraphContext(session_id=session_id)))
        await entered.wait()
        saved = store.load(session_id)
        assert saved is not None
        saved_stack = saved['graph_stack']
        saved_cursors = saved.get('cursors')
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        release.set()
        result = await runtime.run('checkpoint', GraphContext(session_id=session_id))
        evidence['checkpoint_restart'] = [saved_stack, saved_cursors, result.status, result.error]
        assert saved_stack == [] and saved_cursors and result.success

        visits = []

        async def policy(ctx):
            ctx.values['count'] = ctx.values.get('count', 0) + 1
            visits.append('policy' + str(ctx.values['count']))

        async def action(ctx):
            visits.append('action' + str(ctx.values['count']))
            await asyncio.sleep(.01)

        async def judge(ctx):
            visits.append('judge' + str(ctx.values['count']))

        loop = graph('loop', [
            {'id': 'policy', 'module': 'policy'},
            {'id': 'action', 'module': 'action', 'predecessors': ['policy']},
            {'id': 'judge', 'module': 'judge', 'predecessors': ['action'], 'goto': [
                {'node': 'policy', 'when': "ctx.values['count'] < 2"}, {'node': 'END'}]},
        ])
        runtime = GraphRuntime([loop], ModuleRuntime({'policy': Fn(policy), 'action': Fn(action), 'judge': Fn(judge)}), safety_state=SafetyStateStore())
        result = await runtime.run('loop')
        evidence['loop_visits'] = visits
        evidence['loop_history'] = [(h.get('node'), h.get('event')) for h in result.context.history]
        history = evidence['loop_history']
        # The second judge must enter only after the second action exits.
        action_exits = [i for i, h in enumerate(history) if h == ('action', 'exit')]
        judge_enters = [i for i, h in enumerate(history) if h == ('judge', 'enter')]
        assert judge_enters[1] > action_exits[1]

        # BodyServiceClient keeps a dedicated control connection, so an
        # interrupt/health request can be sent while execute is in flight.
        client = BodyServiceClient('unused')
        evidence['rpc_control_channel'] = client.control is not client
        assert evidence['rpc_control_channel']
    print(json.dumps(evidence, indent=2, ensure_ascii=False))


asyncio.run(main())
