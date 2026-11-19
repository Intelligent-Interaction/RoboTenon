import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from robotenon.infra.params import Params
from robotenon.runtime.config import ToolConfig, ToolsConfig, ToolDeploymentConfig
from robotenon.runtime.factory import build_tool_runtime
from robotenon.runtime.model_services import service_command
from robotenon.runtime.runner import EpisodeRequest, _graph_selector
from robotenon.tools.base import ToolResult
from robotenon.tools.registry import register_tool


@register_tool("decoupling-xiaomi-named-utility")
class BoundTool:
    def __init__(self, **kwargs):
        self.arguments = kwargs

    async def call(self, arguments, context):
        return ToolResult(output=self.arguments)


def test_bindings_override_false_and_preserve_missing_defaults():
    spec = ToolConfig(
        name="decoupling-xiaomi-named-utility", role="utility",
        params={"dry_run": True, "port": 42, "path": "fallback"},
        parameter_bindings={"dry_run": "runtime.dry_run", "port": "model.port", "path": "model.path"},
    )
    runtime = build_tool_runtime(ToolsConfig(tools=[spec]), runtime_params=Params({
        "runtime": {"dry_run": False}, "model": {"port": 0},
    }))
    assert runtime.resolve(spec.name).arguments == {"dry_run": False, "port": 0, "path": "fallback"}
    assert runtime._metadata[spec.name]["role"] == "utility"
    asyncio.run(runtime.close())
    with pytest.raises(ValueError, match="runtime_params"):
        build_tool_runtime(ToolsConfig(tools=[spec]))


def test_names_do_not_assign_tool_roles():
    spec = ToolConfig(name="decoupling-xiaomi-named-utility")
    runtime = build_tool_runtime(ToolsConfig(tools=[spec]))
    assert runtime._metadata[spec.name]["role"] == "tool"
    asyncio.run(runtime.close())


def test_external_deployment_provider_without_runtime_changes(monkeypatch):
    module = types.ModuleType("custom_deployment_fixture")
    module.launch = lambda config, host, port: ["server", host, str(port)]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    config = ToolDeploymentConfig(backend="custom_deployment_fixture:launch", gpu_ids=[0])
    assert service_command(config, "localhost", 321) == ["server", "localhost", "321"]
    module.launch = lambda *args: "shell command"
    with pytest.raises(TypeError, match="argv"):
        service_command(config, "localhost", 321)
    with pytest.raises(ValueError, match="module:callable"):
        service_command(ToolDeploymentConfig(backend="unknown", gpu_ids=[0]), "localhost", 321)


def test_multiple_graphs_never_select_by_vendor_or_monitor():
    graphs = {"xr365_single_task": object(), "xr365_single_task_reference": object()}
    loaded = SimpleNamespace(graphs=SimpleNamespace(graph_map=lambda: graphs))
    for enabled in [True, False]:
        with pytest.raises(ValueError, match="runtime.graph_id"):
            _graph_selector(loaded, Params({"runtime": {"monitor_enabled": enabled}}), EpisodeRequest())
    assert _graph_selector(loaded, Params({}), EpisodeRequest(graph_id="xr365_single_task")) == "xr365_single_task"
