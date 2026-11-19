import asyncio
from pathlib import Path

from robotenon.runtime.config import (
    ModuleConfig,
    ModulesConfig,
    ToolConfig,
    ToolsConfig,
    load_tools_config,
)
from robotenon.runtime.factory import (
    build_module_runtime,
    build_tool_runtime,
    initialize_resident_runtimes,
    initialize_tool_runtime,
)
from robotenon.runtime.registry import register_module
from robotenon.schemas import MemoryRecord
from robotenon.tools import (
    ToolContext,
    ToolResult,
    load_builtin_tools,
    register_tool,
    tool_registry,
)
from robotenon.workflow import GraphContext, GraphRuntime
from robotenon.workflow.definition import GraphDefinition


def test_builtin_tools_are_registered_once():
    load_builtin_tools()
    first = tool_registry.describe()
    load_builtin_tools()
    assert tool_registry.describe() == first
    assert {"model", "memory", "skill", "echo"} <= set(tool_registry.names())


def test_manifest_builds_resident_model_memory_and_skill_tools(tmp_path):
    config = ToolsConfig(
        tools=[
            ToolConfig(name="model", id="model_main", params={"backend": "static", "response": {"ok": 1}}),
            ToolConfig(name="memory", id="memory_main", params={"path": str(tmp_path / "memory.db")}),
            ToolConfig(name="skill", id="skill_main"),
        ]
    )

    async def scenario():
        runtime = await initialize_tool_runtime(config)
        model = await runtime.call("model_main", {"prompt": "ignored"})
        record = MemoryRecord(task_id="pick", summary="picked the object", outcome="success")
        stored = await runtime.call("memory", {"op": "add", "record": record.model_dump(mode="json")})
        found = await runtime.call("memory_main", {"op": "search", "task_id": "pick"})
        heads = await runtime.call("skill_main", {"op": "heads"})
        same = runtime.resolve("memory") is runtime.resolve("memory_main")
        await runtime.close()
        return model, stored, found, heads, same

    model, stored, found, heads, same = asyncio.run(scenario())
    assert model.ok and model.output == {"ok": 1}
    assert stored.output["stored"] is True
    assert found.output["count"] == 1
    assert found.output["records"][0].task_id == "pick"
    assert heads.output["skills"] == []
    assert same


def test_tool_lifecycle_constructs_once():
    state = {"constructed": 0, "started": 0, "closed": 0, "calls": 0}

    @register_tool("lifecycle-tool-test", kind="utility")
    class LifecycleTool:
        def __init__(self):
            state["constructed"] += 1

        async def start(self):
            state["started"] += 1

        async def close(self):
            state["closed"] += 1

        async def call(self, arguments, context: ToolContext):
            state["calls"] += 1
            return ToolResult(output={"arguments": dict(arguments), "session": context.session_id})

    async def scenario():
        runtime = await initialize_tool_runtime(
            ToolsConfig(tools=[ToolConfig(name="lifecycle-tool-test", id="resident")])
        )
        first = await runtime.call("resident", {"n": 1})
        second = await runtime.call("lifecycle-tool-test", {"n": 2})
        await runtime.close()
        return first, second

    first, second = asyncio.run(scenario())
    assert first.output["arguments"] == {"n": 1}
    assert second.output["arguments"] == {"n": 2}
    assert state == {"constructed": 1, "started": 1, "closed": 1, "calls": 2}


def test_graph_context_exposes_same_resident_tools():
    @register_module("tool-using-module-test", kind="policy")
    class ToolUsingModule:
        async def run(self, ctx):
            result = await ctx.call_tool("echo", {"value": 3})
            return {"tool_output": result.output}

    async def scenario():
        tools = build_tool_runtime(
            ToolsConfig(tools=[ToolConfig(name="echo", id="echo_main", params={"prefix": "x-"})])
        )
        modules = build_module_runtime(
            ModulesConfig(modules=[ModuleConfig(name="tool-using-module-test")]),
            tools=tools,
        )
        graph = GraphDefinition.model_validate(
            {
                "id": "tool-graph",
                "entry": "call",
                "nodes": [{'id': 'call', 'module': 'tool-using-module-test', 'next': [{'node': 'END'}]}],
            }
        )
        ctx = GraphContext(session_id="session-1")
        runtime = GraphRuntime({graph.id: graph}, modules, tools=tools, backend="fallback")
        result = await runtime.run("tool-graph", ctx=ctx)
        same = ctx.tools is tools
        await modules.close()
        await tools.close()
        return result, ctx, same

    result, ctx, same = asyncio.run(scenario())
    assert result.success, result.error
    assert same
    assert ctx.results["call"]["tool_output"]["prefix"] == "x-"
    assert "tools" not in ctx.to_dict()


def test_tools_manifest_file_is_loadable():
    path = Path(__file__).parents[1] / "configs" / "tools_configs.json"
    config = load_tools_config(path)
    runtime = build_tool_runtime(config)
    assert {"model_main", "memory_main", "skill_main"} <= set(runtime.names())


def test_resident_bootstrap_starts_tools_before_modules():
    order: list[str] = []

    @register_tool("bootstrap-order-tool", kind="utility")
    class BootstrapTool:
        async def start(self):
            order.append("tool")

        async def close(self):
            order.append("tool-close")

        async def call(self, arguments, context):
            return {"ok": True}

    @register_module("bootstrap-order-module", kind="utility")
    class BootstrapModule:
        def __init__(self, tools=None):
            # The factory intentionally only passes configured params. The
            # runtime reference is injected at call time, not constructor time.
            self.tools = tools

        async def start(self):
            order.append("module")

        async def close(self):
            order.append("module-close")

        async def run(self, ctx):
            return {}

    async def scenario():
        tools, modules = await initialize_resident_runtimes(
            ModulesConfig(modules=[ModuleConfig(name="bootstrap-order-module")]),
            ToolsConfig(tools=[ToolConfig(name="bootstrap-order-tool")]),
        )
        started = tools.started and modules.started
        await modules.close()
        await tools.close()
        return started

    assert asyncio.run(scenario())
    assert order == ["tool", "module", "module-close", "tool-close"]
