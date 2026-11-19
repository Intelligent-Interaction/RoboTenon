"""Keep shipped graph manifests on canonical next/goto/loop syntax."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from test_graph_parallel_routing import FunctionModule

from robotenon.modules import ModuleRuntime
from robotenon.runtime.safety import SafetyStateStore
from robotenon.workflow import GraphRuntime
from robotenon.workflow.compiler import compile_graphs
from robotenon.workflow.definition import load_graphs_config

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / 'configs'
CONFIG_ROOTS = (
    CONFIGS,
    ROOT / 'extensions' / 'benchmarks' / 'robocasa365' / 'configs',
    ROOT / 'extensions' / 'benchmarks' / 'robocasa-hlp' / 'configs',
)


def shipped_configs():
    paths = set()
    for root in CONFIG_ROOTS:
        paths.update(root.rglob('*.json'))
    yield from sorted(paths)


def test_all_shipped_graph_manifests_use_current_routes_and_compile():
    manifests = []
    async def work(ctx):
        return {}
    modules = SimpleNamespace(resolve=lambda name: FunctionModule(work), describe=lambda: [])
    for path in shipped_configs():
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or not isinstance(value.get('graphs'), list):
            continue
        manifests.append(path)
        for graph in value['graphs']:
            assert not graph.get('edges'), path
            for node in graph['nodes']:
                for operation in ('next', 'goto'):
                    rules = node.get(operation, [])
                    rules = rules if isinstance(rules, list) else [rules]
                    for rule in rules:
                        assert isinstance(rule, dict) and set(rule) <= {'node', 'when'}, (path, node['id'])
                        assert 'node' in rule
                        if operation == 'goto':
                            assert rule['node'] != node['id'], 'Use loop for self repetition in shipped manifests'
                if 'loop' in node:
                    assert isinstance(node['loop'], (bool, dict))
                    if isinstance(node['loop'], dict):
                        assert set(node['loop']) <= {'every', 'when'}
        compile_graphs(load_graphs_config(path).graph_map(), modules)
    assert manifests
    # Every master graph reference resolves to a migrated/validated manifest.
    for path in shipped_configs():
        value = json.loads(path.read_text())
        if isinstance(value, dict) and isinstance(value.get('graphs_config'), str):
            target = (path.parent / value['graphs_config']).resolve()
            assert target in [p.resolve() for p in manifests], path
            graph_ids = load_graphs_config(target).graph_map()
            graph_id = value.get('runtime', {}).get('runner', {}).get('graph_id')
            if graph_id is not None:
                assert graph_id in graph_ids, path


def test_demo_self_loop_repeats_once_even_when_entered_after_other_nodes():
    async def scenario():
        seen = []
        async def work(ctx):
            if ctx.current_node == 'step':
                seen.append(ctx.trigger)
            return {}
        graphs = load_graphs_config(CONFIGS / 'graphs.json').graph_map()
        runner = GraphRuntime([graphs['demo'], graphs['repeat_once']],
                             ModuleRuntime({'debug_echo': FunctionModule(work)}),
                             safety_state=SafetyStateStore(), backend='fallback')
        result = await runner.run('demo')
        assert result.success, result.error
        assert seen == ['entry', 'loop']
    asyncio.run(scenario())
