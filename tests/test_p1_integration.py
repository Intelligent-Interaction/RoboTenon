"""Regression coverage for the 2026-09-18 independent P1 audit."""

import asyncio
import http.client
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_execution_backends import make_config

from robotenon.monitors.registry import register_monitor
from robotenon.runtime.factory import initialize_application
from robotenon.runtime.runner import EpisodeRequest, run_episode
from robotenon.schemas import SafetyLevel, SafetySignal
from robotenon.storage.safety import SQLiteSafetyStateStore
from robotenon.web.server import build_server


@pytest.mark.parametrize("backend", ["in_process", "process"])
def test_runner_preserves_external_p4_with_monitor_disabled(tmp_path, backend):
    config = make_config(tmp_path, backend=backend, trials=1)
    value = json.loads(config.read_text())
    safety_path = tmp_path / "device-safety.sqlite3"
    value["safety_state"] = {
        "backend": "sqlite",
        "path": str(safety_path),
        "require_shared": True,
    }
    config.write_text(json.dumps(value))
    store = SQLiteSafetyStateStore(safety_path)
    store.update(
        [
            SafetySignal(
                code="device.estop", level=SafetyLevel.P4, source="device", latched=True
            )
        ]
    )
    result = asyncio.run(
        run_episode(EpisodeRequest(config=config, output=tmp_path / "episode.json"))
    )
    assert result["graph_success"] is False
    assert result["context"]["safety"]["level"] == "P4"
    assert store.snapshot().level == SafetyLevel.P4


@pytest.mark.parametrize("scope,level", [("shared", "P4"), ("task", "P0")])
def test_resource_root_only_relocates_explicit_task_safety(tmp_path, scope, level):
    config = make_config(tmp_path)
    value = json.loads(config.read_text())
    safety_path = tmp_path / "shared.sqlite3"
    value["safety_state"] = {
        "backend": "sqlite",
        "path": str(safety_path),
        "scope": scope,
    }
    config.write_text(json.dumps(value))
    store = SQLiteSafetyStateStore(safety_path)
    store.update(
        [
            SafetySignal(
                code="external", level=SafetyLevel.P4, source="device", latched=True
            )
        ]
    )

    async def scenario():
        app = await initialize_application(config, resource_root=tmp_path / "attempt")
        try:
            assert app.safety_state.snapshot().level.value == level
        finally:
            await app.close()

    asyncio.run(scenario())
    assert store.snapshot().level == SafetyLevel.P4
    assert (tmp_path / "attempt/safety.sqlite3").exists() == (scope == "task")


MONITOR_EVENTS = []


@register_monitor("p1-integration-monitor")
class P1Monitor:
    async def start(self):
        MONITOR_EVENTS.append("start")

    async def check(self, feedback=None, *, request=None, context=None):
        MONITOR_EVENTS.append("check")
        return {
            "compliant": False,
            "safety_level": "P4",
            "source": "test",
            "reason": "blocked",
        }

    async def close(self):
        MONITOR_EVENTS.append("close")


@pytest.mark.parametrize("mode", ["request", "resident"])
def test_runner_uses_registered_monitor(tmp_path, mode):
    MONITOR_EVENTS.clear()
    config = make_config(tmp_path, trials=1)
    value = json.loads(config.read_text())
    value["monitors"] = [
        {"name": "p1-integration-monitor", "id": "test-monitor", "modes": [mode]}
    ]
    value["parameters"]["runtime"]["monitor_enabled"] = True
    if mode == "request":
        value["graphs"][0]["nodes"][0]["next"] = ["echo"]
        value["graphs"][0]["nodes"][1]["next"] = ["check"]
        value["graphs"][0].pop("edges", None)
        value["graphs"][0]["nodes"][1]["predecessors"] = ["reset"]
        value["graphs"][0]["nodes"].append(
            {
                "id": "check",
                "type": "system",
                "operation": "monitor.request.test-monitor",
                "predecessors": ["echo"],
            }
        )
    config.write_text(json.dumps(value))
    result = asyncio.run(
        run_episode(EpisodeRequest(config=config, output=tmp_path / "result.json"))
    )
    assert result["graph_success"] is False
    assert result["context"]["safety"]["level"] == "P4"
    assert MONITOR_EVENTS.count("start") == 1
    assert "check" in MONITOR_EVENTS
    assert MONITOR_EVENTS.count("close") == 1


@contextmanager
def portal(*, control=True, allowed_hosts=()):
    calls = []

    class Manager:
        def shutdown_all(self, *, reason, wait=True, source=None):
            calls.append(reason)
            return {"stopped": True}

    server = build_server(
        "127.0.0.1",
        0,
        SimpleNamespace(traces=None),
        experiment_manager=Manager() if control else None,
        allowed_hosts=allowed_hosts,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def post(server, headers, body='{"reason":"test"}'):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        client.request("POST", "/api/system/shutdown", body=body, headers=headers)
        response = client.getresponse()
        return (
            response.status,
            response.getheader("Access-Control-Allow-Origin"),
            response.read(),
        )
    finally:
        client.close()


@pytest.mark.parametrize(
    "headers,status,should_call",
    [
        (
            {"Origin": "https://untrusted.example", "Content-Type": "text/plain"},
            403,
            False,
        ),
        ({"Content-Type": "application/json"}, 200, True),
        (
            {"Content-Type": "application/json", "Authorization": "Bearer wrong"},
            200,
            True,
        ),
        ({"Host": "rebind.example", "Content-Type": "application/json"}, 403, False),
        ({"Origin": "null", "Content-Type": "application/json"}, 403, False),
    ],
)
def test_portal_enforces_origin_but_not_control_tokens(headers, status, should_call):
    with portal() as (server, calls):
        actual, cors, _ = post(server, headers)
        assert actual == status
        assert cors is None
        assert calls == (["test"] if should_call else [])


def test_portal_control_without_token_and_input_limits():
    with portal() as (server, calls):
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{server.server_port}",
        }
        assert server.control_token is None
        assert post(server, headers)[0] == 200
        assert calls == ["test"]
        assert post(server, {**headers, "Content-Type": "text/plain"})[0] == 415
        assert post(server, headers, "[]")[0] == 400
        assert (
            post(server, {**headers, "Content-Length": str(1024 * 1024 + 1)})[0] == 413
        )
        assert calls == ["test"]
        # The secret is not exposed in the publicly readable HTML.
        client = http.client.HTTPConnection("127.0.0.1", server.server_port)
        client.request("GET", "/")
        response = client.getresponse()
        assert response.status == 200
        assert b"access_token" not in response.read()
        client.close()


def test_portal_supports_forwarded_port_and_explicit_hostname():
    with portal(allowed_hosts=("robot.example",)) as (server, calls):
        headers = {
            "Content-Type": "application/json",
            "Host": "localhost:18765",
            "Origin": "http://localhost:18765",
        }
        assert post(server, headers)[0] == 200
        headers.update(Host="robot.example:18765", Origin="https://robot.example:18765")
        assert post(server, headers)[0] == 200
        assert len(calls) == 2


def test_readonly_portal_keeps_control_disabled():
    with portal(control=False) as (server, calls):
        assert server.control_token is None
        assert post(server, {"Content-Type": "application/json"})[0] == 503
        assert calls == []


def test_runner_injects_shared_p4_into_body(tmp_path):
    from robotenon.bodies.registry import register_body
    from robotenon.services.body import InProcessBodyService

    states = []

    @register_body("p1-safety-body", replace=True)
    class SafetyBody(InProcessBodyService):
        def __init__(self, **runtime):
            states.append(runtime["safety_state"])
            super().__init__(lambda action: None, safety_state=runtime["safety_state"])

    config = make_config(tmp_path, trials=1)
    value = json.loads(config.read_text())
    value["bodies"] = [{"name": "p1-safety-body"}]
    path = tmp_path / "device.sqlite3"
    value["safety_state"] = {
        "backend": "sqlite",
        "path": str(path),
        "require_shared": True,
    }
    config.write_text(json.dumps(value))
    store = SQLiteSafetyStateStore(path)
    store.update(
        [
            SafetySignal(
                code="estop", source="device", level=SafetyLevel.P4, latched=True
            )
        ]
    )
    result = asyncio.run(
        run_episode(EpisodeRequest(config=config, output=tmp_path / "result.json"))
    )
    assert result["graph_success"] is False
    assert len(states) == 1
    assert isinstance(states[0], SQLiteSafetyStateStore)
    assert states[0].snapshot().level == SafetyLevel.P4


def test_monitor_disabled_does_not_bypass_require_shared_validation(tmp_path):
    config = make_config(tmp_path)
    value = json.loads(config.read_text())
    value["safety_state"] = {"backend": "memory", "require_shared": True}
    config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="require_shared"):
        asyncio.run(
            run_episode(EpisodeRequest(config=config, output=tmp_path / "result.json"))
        )


def test_disabled_monitor_does_not_clear_external_heartbeat(tmp_path, monkeypatch):
    config = make_config(tmp_path, trials=1)
    value = json.loads(config.read_text())
    value["monitors"] = [
        {
            "name": "p1-integration-monitor",
            "id": "external-monitor",
            "modes": ["resident"],
        }
    ]
    value["safety_state"] = {
        "backend": "sqlite",
        "path": str(tmp_path / "safety.sqlite3"),
    }
    config.write_text(json.dumps(value))
    cleared = []
    original = SQLiteSafetyStateStore.clear_heartbeat

    def clear(store, name):
        cleared.append(name)
        return original(store, name)

    monkeypatch.setattr(SQLiteSafetyStateStore, "clear_heartbeat", clear)
    result = asyncio.run(
        run_episode(EpisodeRequest(config=config, output=tmp_path / "result.json"))
    )
    assert result["graph_success"] is True
    assert cleared == []
