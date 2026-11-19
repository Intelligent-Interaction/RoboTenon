import asyncio
from pathlib import Path

import pytest

from robotenon.runtime.config import ModuleConfig, ModulesConfig, load_modules_config
from robotenon.runtime.factory import build_module_runtime, initialize_module_runtime
from robotenon.runtime.registry import module_registry, register_module


def test_builtins_are_registered_once():
    from robotenon.runtime.registry import load_builtin_components
    load_builtin_components()
    first = module_registry.describe()
    load_builtin_components()
    assert module_registry.describe() == first
    assert {"echo", "terminal-evaluator", "wait-agent"} <= set(module_registry.names())


def test_modules_manifest_builds_resident_instances():
    path = Path(__file__).parents[1] / "configs" / "modules_configs.json"
    config = load_modules_config(path)
    runtime = build_module_runtime(config)
    assert {"debug_echo", "evaluator_main", "agent_main"} <= set(runtime.names())

    async def scenario():
        result = await runtime.call("debug_echo", {"value": 1})
        await runtime.close()
        return result

    result = asyncio.run(scenario())
    assert result.ok and result.output["arguments"] == {"value": 1}


def test_module_lifecycle_constructs_each_instance_once():
    state = {"constructed": 0, "started": 0, "closed": 0}

    @register_module("lifecycle-test", kind="tool")
    class LifecycleModule:
        def __init__(self):
            state["constructed"] += 1

        async def start(self):
            state["started"] += 1

        async def close(self):
            state["closed"] += 1

        async def invoke(self, arguments, context):
            return {"arguments": dict(arguments)}

    async def scenario():
        runtime = await initialize_module_runtime(
            ModulesConfig(modules=[ModuleConfig(name="lifecycle-test", id="resident")])
        )
        first = await runtime.call("resident", {"n": 1})
        second = await runtime.call("resident", {"n": 2})
        await runtime.close()
        return first, second

    first, second = asyncio.run(scenario())
    assert first.output["arguments"] == {"n": 1}
    assert second.output["arguments"] == {"n": 2}
    assert state == {"constructed": 1, "started": 1, "closed": 1}


def test_registry_is_the_only_extension_api():
    @register_module("common-module-test", kind="memory")
    class CommonModule:
        async def invoke(self, arguments, context):
            return {"ok": True}

    runtime = build_module_runtime(
        ModulesConfig(modules=[ModuleConfig(name="common-module-test")])
    )

    async def scenario():
        return await runtime.call("common-module-test")

    result = asyncio.run(scenario())
    assert result.output == {"ok": True}


def test_non_invokable_module_is_rejected():
    @register_module("not-invokable", kind="memory")
    class NotInvokable:
        pass

    with pytest.raises(TypeError, match="implement invoke.*run"):
        build_module_runtime(ModulesConfig(modules=[ModuleConfig(name="not-invokable")]))


def test_graph_runtime_is_built_from_the_application_manifest():
    from robotenon.runtime.config import load_application_config
    from robotenon.runtime.factory import build_graph_runtime

    loaded = load_application_config(Path(__file__).parents[1] / "configs" / "config.json")
    runtime = build_module_runtime(loaded.modules, tools=None, params=loaded.params)
    graph_runtime = build_graph_runtime(
        loaded.graphs,
        runtime,
        backend="fallback",
        params=loaded.params,
    )
    assert set(graph_runtime.graphs) >= {"demo", "embodied_loop"}
    asyncio.run(runtime.close())
