"""A plugin outside robotenon must work without modifying builtin registries."""
import asyncio
import json

import pytest

from robotenon.runtime.experiment import ExperimentRequest, _adapter_overrides, run_experiment
from robotenon.runtime.runner_cli import parse_args, request_from_args, experiment_request_from_args
from test_execution_backends import make_config


@pytest.mark.parametrize("backend", ["in_process", "process"])
def test_external_environment_and_task_adapter(tmp_path, monkeypatch, backend):
    name = f"external_example_{backend}"
    (tmp_path / f"{name}.py").write_text('''
from robotenon.envs.base import register_environment
from robotenon.envs.base.fake import FakeEnvironmentAdapter
from robotenon.task_adapters import register_task_adapter
from robotenon.task_adapters.single import SingleTaskAdapter
from robotenon.schemas import EnvironmentDescriptor

@register_environment("external-example-env", replace=True)
class ExampleEnvironment(FakeEnvironmentAdapter):
    descriptor = EnvironmentDescriptor(env_id="external-example-env", env_version="1.0")
    async def open(self, spec):
        assert spec.parameters["scene"] == {"layout": "custom", "initial": 7}
        assert spec.parameters["split"] == "validation"
        assert "task_only" not in spec.parameters
        return await super().open(spec)

@register_task_adapter("external-example-task", replace=True)
class ExampleTasks(SingleTaskAdapter):
    pass
''')
    monkeypatch.syspath_prepend(str(tmp_path))
    config = make_config(tmp_path, backend=backend)
    raw = json.loads(config.read_text())
    raw["plugin_environments"] = [name]
    raw["plugin_task_adapters"] = [name]
    raw["environments"] = [{"id": "env", "name": "external-example-env"}]
    raw["task_adapters"] = [{"id": "custom", "name": "external-example-task", "params": {
        "num_trials": 1, "split": "validation", "task_only": "private",
        "environment_parameters": {"scene": {"layout": "custom", "initial": 0}},
    }}]
    raw["parameters"]["environment"] = {"parameters": {"scene": {"layout": "default"}}}
    config.write_text(json.dumps(raw))
    result = asyncio.run(run_experiment(ExperimentRequest(
        config=config, output_dir=tmp_path / "runs", run_id="external",
        task_adapter_overrides={"custom": {
            "num_trials": 2, "environment_parameters": {"scene": {"initial": 3}},
        }},
        environment_parameters={"scene": {"initial": 7}},
    )))
    assert result["num_errors"] == 0, result
    status = json.loads((tmp_path / "runs/external/status.json").read_text())
    assert status["completed_episodes"] == 2


def test_generic_cli_accepts_provider_options_and_split():
    args = parse_args(["--split", "validation", "--environment-parameters", '{"scene": 1}'])
    request = request_from_args(args)
    assert request.split == "validation"
    assert request.environment_parameters == {"scene": 1}
    args = parse_args(["--batch", "--task-adapter-overrides", '{"custom": {"suite": "demo"}}'])
    assert experiment_request_from_args(args).task_adapter_overrides == {"custom": {"suite": "demo"}}
    with pytest.raises(SystemExit):
        parse_args(["--environment-parameters", "[]"])
    with pytest.raises(SystemExit):
        parse_args(["--task-adapter-overrides", "{}"])


def test_legacy_overrides_remain_available_with_explicit_precedence():
    assert _adapter_overrides(ExperimentRequest(num_trials=3, task_adapter_overrides={
        "robocasa365": {"num_trials": 2}, "custom": {"suite": "demo"},
    })) == {"robocasa365": {"num_trials": 2}, "custom": {"suite": "demo"}}


@pytest.mark.parametrize("invalid", [[], {"custom": []}, {"custom": None}])
def test_invalid_adapter_overrides_fail(invalid):
    with pytest.raises(ValueError):
        _adapter_overrides(ExperimentRequest(task_adapter_overrides=invalid))


def test_unknown_override_target_fails(tmp_path):
    with pytest.raises(ValueError, match="unknown task adapter override targets"):
        asyncio.run(run_experiment(ExperimentRequest(
            config=make_config(tmp_path), task_adapter_overrides={"typo": {}},
        )))
