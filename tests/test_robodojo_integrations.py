"""Contract tests for the separately packaged RoboDojo official runner.

Heavy Isaac/RoboDojo imports are deliberately excluded; GPU validation belongs
to the target host smoke test documented by each extension.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from robotenon.bodies.registry import BodyRegistry
from robotenon.envs.base.protocol import EnvironmentRegistry
from robotenon.runtime.config import load_application_config
from robotenon.task_adapters.registry import TaskAdapterRegistry

ROOT = Path(__file__).parents[1]
OFFICIAL = ROOT / "extensions" / "benchmarks" / "robodojo-office"
sys.path.insert(0, str(OFFICIAL / "src"))


def test_extensions_do_not_leak_into_core_registries():
    assert "robodojo" not in EnvironmentRegistry().names()
    assert "robodojo_body" not in BodyRegistry().names()
    assert "robodojo" not in TaskAdapterRegistry().names()
    assert "robodojo-office" not in TaskAdapterRegistry().names()


def test_official_master_config_loads():
    official = load_application_config(
        OFFICIAL / "configs" / "robotenon_robodojo_office.json"
    )
    assert official.task_adapters.task_adapters[0].name == "robodojo-office"


def test_official_runner_uses_explicit_supported_cli_arguments(tmp_path):
    from robotenon_robodojo_office.runner import OfficialRoboDojoRunner, OfficialRunSpec

    script = tmp_path / "scripts" / "robodojo.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    command = OfficialRoboDojoRunner().command(OfficialRunSpec(
        root=tmp_path,
        task_name="stack_bowls",
        policy_name="RoboTenon",
        policy_host="127.0.0.1",
        policy_port=19000,
        env_config="arx_x5",
    ))
    assert command[:3] == ["bash", str(script), "client"]
    assert command[command.index("--env-cfg") + 1] == "arx_x5"
    assert command[command.index("--policy-port") + 1] == "19000"


def test_official_result_lookup_never_falls_back_to_stale_run(tmp_path):
    from robotenon_robodojo_office.results import find_result_file, project_result

    stale = tmp_path / "RoboDojo/stack_bowls/RoboTenon/arx_x5/0_x/old/result.json"
    current = tmp_path / "RoboDojo/stack_bowls/RoboTenon/arx_x5/0_x/run-123/result.json"
    for path, rate in ((stale, 0.0), (current, 0.5)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"success_rate": rate, "eval_time": 2, "score": 50, "details": {}}))
    selected = find_result_file(
        tmp_path, task_name="stack_bowls", policy_name="RoboTenon", run_id="run-123"
    )
    assert selected == current
    assert project_result(selected)["task_success"] is True
    with pytest.raises(FileNotFoundError):
        find_result_file(
            tmp_path, task_name="stack_bowls", policy_name="RoboTenon", run_id="missing"
        )


def test_policy_bridge_requires_observation_and_preserves_batch_identity():
    from robotenon_robodojo_office.policy_bridge import RoboTenonPolicyBridge

    class Executor:
        async def reset(self, **kwargs):
            pass

        async def infer(self, observation, *, env_index=None):
            return [{"env": env_index, "value": observation["value"]}]

        async def trial_end(self, result=None):
            pass

    async def scenario():
        bridge = RoboTenonPolicyBridge(Executor())
        with pytest.raises(RuntimeError, match="before update_obs"):
            await bridge.get_action()
        await bridge.update_obs_batch([{"env_idx": 4, "value": "four"}, {"env_idx": 9, "value": "nine"}])
        return await bridge.get_action_batch([9, 4])

    assert asyncio.run(scenario()) == [
        [{"env": 9, "value": "nine"}],
        [{"env": 4, "value": "four"}],
    ]
