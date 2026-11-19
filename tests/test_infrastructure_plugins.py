import asyncio
import importlib
import json

import pytest

from robotenon.runtime.experiment import ExperimentRequest, run_experiment
from robotenon.runtime.runner import EpisodeRequest, run_episode
from test_execution_backends import make_config


@pytest.fixture(scope="module")
def external_infra(tmp_path_factory):
    directory = tmp_path_factory.mktemp("external_infrastructure")
    name = "example_infrastructure_plugin"
    (directory / f"{name}.py").write_text('''
import asyncio
from robotenon.runtime.execution_registry import register_execution_backend
from robotenon.runtime.execution import SequentialExecutionBackend
from robotenon.storage.registry import register_store
from robotenon.services.transports import register_transport
from robotenon.runtime.safety import SafetyStateStore
from robotenon.storage.context import SQLiteContextStore
from robotenon.storage.actions import SQLiteActionExecutionStore
from robotenon.infra.observation_cache import ObservationCache

events = []

@register_execution_backend("example-inline", capabilities={"local_worker"})
def inline(*, context, marker):
    assert marker == "configured"
    events.append("backend-built")
    return SequentialExecutionBackend()

@register_execution_backend("example-process", capabilities={"shared_observability", "managed_workers"})
def process(*, context, marker):
    assert marker == "configured"
    from robotenon.runtime.parallel import PersistentExecutionBackend
    return PersistentExecutionBackend(context.options, context.profile, context.make_request)

@register_store("safety", "example-safety", capabilities={"shared"})
def safety(*, context, marker):
    class Store(SafetyStateStore):
        async def close(self):
            events.append("safety-closed:" + marker)
    return Store()

@register_store("context", "example-context")
def checkpoint(*, context, filename):
    class Store(SQLiteContextStore):
        def close(self):
            events.append("context-closed")
    return Store(context.path(filename))

@register_store("actions", "example-actions")
def actions(*, context, filename):
    class Store(SQLiteActionExecutionStore):
        async def close(self):
            super().close()
    return Store(context.path(filename))

@register_store("observation", "example-observations")
def observations(*, context, folder):
    return ObservationCache(context.path(folder), workers=1)

@register_transport("example-loopback")
class LoopbackTransport:
    def __init__(self, *, endpoint):
        self.host, port = endpoint.rsplit(":", 1)
        self.port = int(port)

    async def connect(self, *, limit):
        return await asyncio.open_connection(self.host, self.port, limit=limit)

    async def listen(self, handler, *, limit):
        return await asyncio.start_server(handler, self.host, self.port, limit=limit)

    async def close(self):
        events.append("transport-closed")
''')
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(directory))
        yield name, importlib.import_module(name)


@pytest.mark.parametrize("backend", ["example-inline", "example-process"])
@pytest.mark.parametrize("batch", [False, True])
def test_external_execution_backend_works_in_both_entrypoints(tmp_path, external_infra, backend, batch):
    name, _ = external_infra
    config = make_config(tmp_path)
    raw = json.loads(config.read_text())
    raw["parameters"]["runtime"]["execution"].update({
        "backend": backend, "plugin_backends": [name], "backend_params": {"marker": "configured"},
    })
    config.write_text(json.dumps(raw))
    if batch:
        result = asyncio.run(run_experiment(ExperimentRequest(config=config, output_dir=tmp_path / "runs")))
        assert result["num_errors"] == 0, result
    else:
        result = asyncio.run(run_episode(EpisodeRequest(config=config, output=tmp_path / "episode.json")))
        assert result["graph_success"] is True


def test_external_stores_are_configured_and_closed(tmp_path, external_infra):
    from robotenon.runtime.factory import build_observation_cache, build_safety_state
    from robotenon.storage.registry import build_store
    name, plugin = external_infra
    config = make_config(tmp_path)
    raw = json.loads(config.read_text())
    raw["safety_state"] = {"backend": "example-safety", "plugin_stores": [name],
                           "params": {"marker": "success"}, "require_shared": True}
    raw["parameters"]["runtime"]["context_store"] = {
        "backend": "example-context", "plugin_stores": [name], "params": {"filename": "context.db"},
    }
    config.write_text(json.dumps(raw))
    asyncio.run(run_episode(EpisodeRequest(config=config, output=tmp_path / "result.json")))
    assert (tmp_path / "context.db").exists()
    assert "context-closed" in plugin.events
    assert "safety-closed:success" in plugin.events
    cache = build_observation_cache({"backend": "example-observations", "plugin_stores": [name],
                                    "params": {"folder": "observations"}}, base_dir=tmp_path)
    asyncio.run(cache.close())
    actions = build_store("actions", {"backend": "example-actions", "plugin_stores": [name],
                                      "params": {"filename": "actions.db"}}, base_dir=tmp_path)
    asyncio.run(actions.close())
    assert (tmp_path / "actions.db").exists()
    with pytest.raises(ValueError, match="shared"):
        build_safety_state({"backend": "memory", "require_shared": True})


def test_external_transport_preserves_service_rpc(tmp_path, external_infra):
    from robotenon.services.ipc import EnvironmentServiceClient, EnvironmentServiceServer
    from robotenon.envs.base.fake import FakeEnvironmentSession
    from robotenon.schemas import SessionSpec
    name, _ = external_infra

    async def scenario():
        session = FakeEnvironmentSession(SessionSpec(task_id="transport", environment_id="fake"))
        server = EnvironmentServiceServer("127.0.0.1:0", session,
            transport="example-loopback", plugin_transports=[name])
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        client = EnvironmentServiceClient(f"127.0.0.1:{port}",
            transport="example-loopback", plugin_transports=[name])
        try:
            feedback = await client.reset(session.spec)
            assert feedback.session_id == session.session_id
            assert (await client.observe()).session_id == session.session_id
        finally:
            await client.close_connection()
            await server.close()
    asyncio.run(scenario())


def test_body_control_channel_uses_configured_transport(external_infra):
    from robotenon.services.ipc import BodyServiceClient, BodyServiceServer
    from robotenon.services.body import InProcessBodyService
    from robotenon.runtime.safety import SafetyStateStore
    from robotenon.schemas import Action, ActionChunk
    from uuid import uuid4
    name, _ = external_infra

    async def scenario():
        body = InProcessBodyService(lambda action: None, safety_state=SafetyStateStore())
        server = BodyServiceServer("127.0.0.1:0", body,
            transport="example-loopback", plugin_transports=[name])
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        client = BodyServiceClient(f"127.0.0.1:{port}",
            transport="example-loopback", plugin_transports=[name])
        try:
            await client.start()
            session_id = uuid4()
            result = await client.execute(ActionChunk(session_id=session_id,
                actions=[Action(session_id=session_id)], max_duration_ms=1000))
            assert result.executed_actions == 1
            assert (await client.health()).ready
            assert client._writer is not client.control._writer
        finally:
            await client.close()
            await body.close()
            await server.close()
    asyncio.run(scenario())


def test_custom_sensor_modality_survives_cache_and_resolver(tmp_path):
    from robotenon.infra import ObservationCache, ObservationPipeline
    from robotenon.schemas import ObservationFrame

    async def scenario():
        cache = ObservationCache(tmp_path)
        try:
            pipeline = ObservationPipeline(cache)
            result = await pipeline.ingest({"session_id": "sensor", "frames": [ObservationFrame(
                frame_id="event_sensor", role="policy_input", modality="vendor.event_stream",
                producer="vendor.sensor_bridge", data=b"events", encoding="binary",
            )]})
            ref = result.frames["event_sensor"]
            assert ref.modality == "vendor.event_stream"
            assert result.observation.channels[0].modality == "vendor.event_stream"
            assert await pipeline.resolver.resolve_ref(ref) == b"events"
        finally:
            await cache.close()
    asyncio.run(scenario())


def test_unknown_backend_does_not_fall_back():
    from robotenon.infra.params import Params
    from robotenon.runtime.execution import ExecutionOptions
    with pytest.raises(ValueError, match="unknown execution"):
        ExecutionOptions.from_params(Params({"runtime": {"execution": {"backend": "typo"}}}))


def test_owned_store_closed_when_application_start_fails(tmp_path, external_infra):
    from robotenon.runtime.factory import initialize_application
    name, plugin = external_infra
    raw = json.loads(make_config(tmp_path).read_text())
    raw["safety_state"] = {"backend": "example-safety", "plugin_stores": [name],
                           "params": {"marker": "failure"}}
    raw["bodies"] = [{"name": "in-process"}]
    with pytest.raises(ValueError, match="executor"):
        asyncio.run(initialize_application(raw, base_dir=tmp_path))
    assert plugin.events.count("safety-closed:failure") == 1


def test_body_configured_action_store_closes_and_reopens(tmp_path, external_infra):
    from robotenon.services.body import InProcessBodyService
    from robotenon.runtime.safety import SafetyStateStore
    name, _ = external_infra

    async def scenario():
        body = InProcessBodyService(lambda action: None, safety_state=SafetyStateStore(),
            idempotency_store_config={"backend": "example-actions", "plugin_stores": [name],
                                      "params": {"filename": str(tmp_path / "ledger.db")}})
        await body.start()
        first = body.idempotency_store
        await body.close()
        assert first._closed
        await body.start()
        assert body.idempotency_store is not first
        await body.close()
        assert body.idempotency_store._closed
    asyncio.run(scenario())


def test_failed_server_start_does_not_remove_live_endpoint():
    import tempfile
    from pathlib import Path
    from robotenon.services.ipc import JsonRpcClient, JsonRpcServer

    async def scenario():
        path = Path(directory) / "service.sock"
        first = JsonRpcServer(path, {"ping": lambda params: "pong"})
        second = JsonRpcServer(path, {})
        await first.start()
        client = JsonRpcClient(path)
        try:
            with pytest.raises(RuntimeError, match="already active"):
                await second.start()
            await second.close()
            assert await client.call("ping") == "pong"
        finally:
            await client.close_connection()
            await first.close()
        assert not path.exists()
    with tempfile.TemporaryDirectory(prefix="rpc-", dir="/tmp") as directory:
        asyncio.run(scenario())
