import asyncio
from pathlib import Path
from uuid import uuid4

from robotenon.bodies import body_registry, load_builtin_bodies, register_body
from robotenon.runtime.config import (
    BodyConfig,
    BodiesConfig,
    EnvironmentConfig,
    EnvironmentsConfig,
    load_application_config,
)
from robotenon.runtime.factory import (
    build_environment_adapter,
    initialize_body_service,
)
from robotenon.schemas import BodyExecutionResult, EnvironmentDescriptor, EnvironmentHealth


def test_builtin_body_factories_are_explicitly_loaded():
    load_builtin_bodies()
    assert {"in-process", "in_process", "ipc", "uds"} <= set(body_registry.names())


def test_body_registry_builds_and_starts_one_resident_service():
    name = f"body-test-{uuid4()}"
    state = {"constructed": 0, "started": 0, "closed": 0, "runtime": None}

    @register_body(name, kind="test")
    class Body:
        def __init__(self, *, endpoint: str, marker=None):
            state["constructed"] += 1
            state["runtime"] = marker
            self.endpoint = endpoint

        async def start(self):
            state["started"] += 1

        async def execute(self, action_chunk):
            return BodyExecutionResult(chunk_id=action_chunk.chunk_id, session_id=action_chunk.session_id)

        async def interrupt(self, reason: str, *, safe_action=None):
            return BodyExecutionResult(status="interrupted", interrupted=True, error=reason)

        async def health(self):
            return EnvironmentHealth(ready=True)

        async def close(self):
            state["closed"] += 1

    config = BodiesConfig(
        bodies=[BodyConfig(name=name, id="body-main", kind="test", params={"endpoint": "robot"})]
    )

    async def scenario():
        service = await initialize_body_service(config, runtime={"marker": "injected"})
        await service.close()

    asyncio.run(scenario())
    assert state == {"constructed": 1, "started": 1, "closed": 1, "runtime": "injected"}


def test_environment_adapter_selection_uses_the_independent_registry():
    name = f"env-test-{uuid4()}"

    class Adapter:
        descriptor = EnvironmentDescriptor(env_id=name, env_version="2.0.0")

        def __init__(self, *, endpoint: str):
            self.endpoint = endpoint

        def describe(self):
            return self.descriptor

        async def connect(self):
            return None

    from robotenon.envs import register_environment

    register_environment(name, Adapter)
    adapter = build_environment_adapter(
        EnvironmentsConfig(
            environments=[
                EnvironmentConfig(
                    id="env-main",
                    name=name,
                    version="2.0.0",
                    params={"endpoint": "127.0.0.1"},
                )
            ]
        )
    )
    assert adapter.endpoint == "127.0.0.1"


def test_master_manifest_exposes_body_and_environment_sections():
    loaded = load_application_config(Path(__file__).parents[1] / "configs" / "config.json")
    assert loaded.bodies.bodies == []
    assert loaded.environments.environments == []
