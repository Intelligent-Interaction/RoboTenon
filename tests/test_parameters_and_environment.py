import asyncio
import json
from pathlib import Path

import pytest

from robotenon.envs import bind_environment, environment
from robotenon.envs.base.fake import FakeEnvironmentAdapter
from robotenon.infra.params import (
    ParameterConflictError,
    ParameterError,
    ParameterTypeError,
    Params,
    load_config_bundle,
)
from robotenon.runtime.config import ModuleConfig, ModulesConfig
from robotenon.runtime.factory import build_module_runtime
from robotenon.runtime.registry import register_module
from robotenon.schemas import SessionSpec
from robotenon.workflow import GraphContext, GraphDefinition, GraphRuntime


def test_params_merges_experiment_and_validates_contract(tmp_path: Path):
    overall = tmp_path / "overall.json"
    experiment = tmp_path / "experiment.json"
    overall.write_text(
        json.dumps({"policy": {"timeout_ms": 1000}, "items": ["base"]}),
        encoding="utf-8",
    )
    experiment.write_text(
        json.dumps({"parameters": {"policy": {"timeout_ms": 750}, "enabled": True}}),
        encoding="utf-8",
    )

    params = Params.load(overall, experiment)
    assert params.get("policy.timeout_ms", [int, 0]) == 750
    assert params.get("enabled", [bool, False]) is True
    assert params.get("missing", [str, "default"]) == "default"
    # Lists are leaves and are replaced rather than concatenated.
    assert params.get("items", [list, []]) == ["base"]
    with pytest.raises(ParameterTypeError):
        params.get("policy.timeout_ms", [str, ""])


def test_master_config_strictly_rejects_duplicate_child_keys(tmp_path: Path):
    (tmp_path / "modules.json").write_text(
        json.dumps(
            {
                "config_version": 1,
                "modules": [],
                "parameters": {"shared": {"x": 1}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "graphs.json").write_text(
        json.dumps(
            {
                "config_version": 1,
                "graphs": [],
                "parameters": {"shared": {"x": 2}},
            }
        ),
        encoding="utf-8",
    )
    master = tmp_path / "config.json"
    master.write_text(
        json.dumps(
            {
                "config_version": 1,
                "modules_config": "modules.json",
                "graphs_config": "graphs.json",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ParameterConflictError, match="shared.x"):
        load_config_bundle(master)


def test_mapping_master_requires_or_uses_explicit_base_dir(tmp_path: Path):
    (tmp_path / "modules.json").write_text(
        json.dumps({"config_version": 1, "modules": []}), encoding="utf-8"
    )
    raw = {"config_version": 1, "modules_config": "modules.json"}
    with pytest.raises(ParameterError, match="base_dir"):
        load_config_bundle(raw)
    bundle = load_config_bundle(raw, base_dir=tmp_path)
    assert bundle.sources["modules"].endswith("modules.json")


def test_runtime_storage_paths_follow_master_config_directory(tmp_path: Path):
    from robotenon.runtime.config import ObservationCacheConfig, SafetyStateConfig
    from robotenon.runtime.factory import build_observation_cache, build_safety_state

    cache = build_observation_cache(
        ObservationCacheConfig.model_validate(
            {
                "storage": {"root": "data/observations", "index": "data/index.sqlite3"},
                "persist": {"workers": 1},
            }
        ),
        base_dir=tmp_path,
    )
    safety = build_safety_state(
        SafetyStateConfig(backend="sqlite", path="data/safety.sqlite3"),
        base_dir=tmp_path,
    )
    assert cache.root == tmp_path / "data/observations"
    assert cache.index_path == tmp_path / "data/index.sqlite3"
    assert safety.path == tmp_path / "data/safety.sqlite3"
    asyncio.run(cache.close())


@register_module("ctx-only-parameter-probe", kind="test", replace=True)
class _ContextOnlyParameterProbe:
    def __init__(self):
        self.environment_seen = None

    async def run(self, ctx):
        self.environment_seen = environment.current
        return {
            "timeout_ms": ctx.params.get("policy.timeout_ms", [int, 0]),
            "same_ctx": isinstance(ctx, GraphContext),
        }


def test_graph_modules_receive_only_ctx_and_resolve_environment_singleton():
    async def scenario():
        adapter = FakeEnvironmentAdapter(max_steps=1)
        session = await adapter.open(
            SessionSpec(task_id="probe", environment_id="fake")
        )
        modules = build_module_runtime(
            ModulesConfig(modules=[ModuleConfig(name="ctx-only-parameter-probe")]),
            params=Params({"policy": {"timeout_ms": 321}}),
        )
        graph = GraphDefinition.model_validate(
            {
                "id": "probe",
                "entry": "probe",
                "nodes": [{"id": "probe", "module": "ctx-only-parameter-probe"}],
            }
        )
        ctx = GraphContext()
        runtime = GraphRuntime(
            {graph.id: graph},
            modules,
            backend="fallback",
            params=Params({"policy": {"timeout_ms": 321}}),
        )
        previous = bind_environment(session)
        try:
            result = await runtime.run("probe", ctx=ctx)
        finally:
            environment.restore(previous)
        probe = modules.resolve("ctx-only-parameter-probe")
        await modules.close()
        await session.close()
        return result, probe

    result, probe = asyncio.run(scenario())
    assert result.success is True
    assert result.context.outputs["probe"]["timeout_ms"] == 321
    assert probe.environment_seen is not None
    assert environment.current is None


def test_environment_binding_is_idempotent_for_an_existing_observation_wrapper():
    from robotenon.services.environment import ObservationEnvironmentService

    class Pipeline:
        async def ingest(self, value, **kwargs):
            return value

    previous = environment.current
    pipeline = Pipeline()
    service = object()
    try:
        bind_environment(service, observation_pipeline=pipeline)
        wrapped = environment.current
        assert isinstance(wrapped, ObservationEnvironmentService)
        old = bind_environment(service, observation_pipeline=pipeline)
        assert old is wrapped
        assert environment.current is wrapped
        # A direct handle binding follows the same idempotent rule.
        assert environment.bind(service) is wrapped
        assert environment.current is wrapped
    finally:
        environment.restore(previous)
