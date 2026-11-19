import asyncio
import json

import pytest

from robotenon.envs.base.fake import FakeEnvironmentAdapter
from robotenon.infra.params import Params
from robotenon.runtime.runner import (
    EpisodeRequest, _apply_request_overrides, _session_parameters, run_episode,
)
from robotenon.task_adapters.base import TaskCase
from test_execution_backends import make_config


def test_provider_options_merge_and_do_not_leak_between_cases():
    defaults = Params({"environment": {"parameters": {
        "suite": "demo", "initial": {"index": 0, "settle": 10},
        "cameras": ["front", "wrist"],
    }}})
    case = TaskCase("custom", "task", "custom/task", 0, parameters={
        "environment_parameters": {"initial": {"index": 3}, "cameras": ["front"]},
        "private_task_note": "not an environment option",
    })
    # Exercise the same JSON-compatible case payload used for task transport.
    payload = json.loads(json.dumps(case.to_dict()))
    payload.pop("key")
    case = TaskCase(**payload)
    merged = _apply_request_overrides(defaults, EpisodeRequest(
        environment_parameters={"initial": {"settle": 5}},
    ), case=case)
    options = _session_parameters(merged, env_id=case.environment_id, split=None)
    assert options == {
        "task_env_id": "custom/task", "suite": "demo",
        "initial": {"index": 3, "settle": 5}, "cameras": ["front"],
    }
    options["initial"]["index"] = 99
    following = _apply_request_overrides(defaults, EpisodeRequest())
    assert _session_parameters(following, env_id="next", split=None)["initial"] == {
        "index": 0, "settle": 10,
    }
    assert merged.get("environment.parameters.initial.index") == 3


def test_canonical_options_keep_existing_precedence():
    params = Params({
        "environment": {"parameters": {
            "task_env_id": "wrong", "split": "wrong", "camera_width": 64,
            "environment_io": {"old": True}, "custom_flag": True,
        }, "io": {"cameras": {}}},
        "render": {"camera_width": 128},
    })
    assert _session_parameters(params, env_id="selected", split="test") == {
        "task_env_id": "selected", "split": "test", "camera_width": 128,
        "environment_io": {"cameras": {}}, "custom_flag": True,
    }


@pytest.mark.parametrize("invalid", [[], "bad", 0, False])
@pytest.mark.parametrize("source", ["config", "case", "request"])
def test_environment_options_require_objects(invalid, source):
    params = Params({"environment": {"parameters": invalid}} if source == "config" else {})
    case = TaskCase("custom", "task", "env", 0, parameters={
        "environment_parameters": invalid,
    }) if source == "case" else None
    request = EpisodeRequest(environment_parameters=invalid) if source == "request" else EpisodeRequest()
    with pytest.raises(ValueError):
        _apply_request_overrides(params, request, case=case)


def test_runner_delivers_custom_options_to_environment_open(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    raw = json.loads(config.read_text())
    raw["parameters"]["environment"] = {"parameters": {"suite": "demo", "initial": 0}}
    config.write_text(json.dumps(raw))
    received = []
    original = FakeEnvironmentAdapter.open

    async def capture(self, spec):
        received.append(spec.parameters)
        return await original(self, spec)

    monkeypatch.setattr(FakeEnvironmentAdapter, "open", capture)
    asyncio.run(run_episode(EpisodeRequest(
        config=config, output=tmp_path / "episode.json",
        environment_parameters={"initial": 4},
    )))
    assert len(received) == 1
    assert received[0]["suite"] == "demo"
    assert received[0]["initial"] == 4
    assert received[0]["task_env_id"] == "fake"
