import asyncio
import json
import sys

import pytest
from test_execution_backends import make_config

from robotenon.infra.params import Params
from robotenon.runtime.config import ToolDeploymentConfig, load_application_config
from robotenon.runtime.execution import ExecutionOptions
from robotenon.runtime.experiment import ExperimentRequest, run_experiment
from robotenon.runtime.model_services import (
    ModelService,
    physical_devices,
    service_command,
)
from robotenon.runtime.parallel import PersistentExecutionBackend
from robotenon.runtime.runner import EpisodeRequest

SERVER = """
import json, os, socket, sys
from pathlib import Path
host,port,record=sys.argv[1:]
Path(record).write_text(json.dumps({'cuda':os.environ.get('CUDA_VISIBLE_DEVICES'), 'pid':os.getpid(),'port':int(port)}))
with socket.socket() as server:
    server.bind((host,int(port)))
    server.listen()
    while True:
        conn,_=server.accept()
        conn.close()
"""
PLUGIN = """
import os
from robotenon.tools.base import BaseTool,ToolResult
from robotenon.tools.registry import register_tool
from robotenon.runtime.registry import register_module
from robotenon.modules.base import BaseModule
from robotenon.task_adapters.single import SingleTaskAdapter
from robotenon.task_adapters import register_task_adapter
@register_tool('deployment-test-client')
class Client(BaseTool):
    def bind_deployment(self,host,port,model_path): self.endpoint=(host,port)
    async def call(self,arguments,context):
        return ToolResult(output={'endpoint':self.endpoint,'cuda':os.environ.get('CUDA_VISIBLE_DEVICES')})
@register_module('deployment-check',kind='test')
class Check(BaseModule):
    async def run(self,ctx):
        a=await ctx.call_tool('xiaomi',{})
        b=await ctx.call_tool('qwen',{})
        ctx.values['deployments']={'xiaomi':a.output,'qwen':b.output}
        return {'success':True}
@register_task_adapter('deployment-crash')
class Crash(SingleTaskAdapter):
    async def run_case(self,case,context):
        os._exit(17)
"""


def deployment_config(tmp_path, monkeypatch, *, crash=False):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    (tmp_path / "model_deploy_fixture.py").write_text(PLUGIN)
    monkeypatch.syspath_prepend(str(tmp_path))
    path = make_config(tmp_path, backend="process", workers=2, trials=4)
    value = json.loads(path.read_text())
    value["plugin_tools"] = value["plugin_modules"] = value["plugin_task_adapters"] = [
        "model_deploy_fixture"
    ]
    value["modules"] = [{"name": "deployment-check", "id": "echo"}]
    execution = value["parameters"]["runtime"]["execution"]
    execution.update(gpu_ids=["10", "11", "12", "13", "14", "15"], startup_timeout_s=20)
    value["tools"] = [
        {
            "id": name,
            "name": "deployment-test-client",
            "deployment": {
                "backend": "command",
                "gpu_ids": ids,
                "startup_timeout_s": 3,
                "shutdown_timeout_s": 1,
                "command": [
                    sys.executable,
                    str(script),
                    "{host}",
                    "{port}",
                    str(tmp_path / (name + "-{port}.json")),
                ],
            },
        }
        for name, ids in [("xiaomi", [0]), ("qwen", [1, 2])]
    ]
    if crash:
        value["task_adapters"][0]["name"] = "deployment-crash"
    path.write_text(json.dumps(value))
    return path


def test_gpu_mapping_and_explicit_egl():
    options = ExecutionOptions.from_params(
        Params(
            {
                "runtime": {
                    "execution": {
                        "backend": "process",
                        "max_workers": 2,
                        "gpu_ids": [4, 5, 6, 7, 8, 9],
                        "egl_device_ids": [2, 3],
                    }
                }
            }
        )
    )
    assert options.worker_gpus(0) == ("4", "5", "6")
    assert options.worker_gpus(1) == ("7", "8", "9")
    assert physical_devices([1, 2], "7,8,9") == ("8", "9")
    backend = PersistentExecutionBackend(options, EpisodeRequest(), lambda *_: None)
    assert [(s.gpu_id, s.egl_device_id) for s in backend.slots] == [
        ("4,5,6", "2"),
        ("7,8,9", "3"),
    ]


@pytest.mark.parametrize("gpu_ids", [[0, 1, 2], [0, 1, 1, 2]])
def test_invalid_pool_rejected(gpu_ids):
    with pytest.raises(ValueError):
        ExecutionOptions.from_params(
            Params(
                {
                    "runtime": {
                        "execution": {
                            "backend": "process",
                            "max_workers": 2,
                            "gpu_ids": gpu_ids,
                        }
                    }
                }
            )
        )




@pytest.mark.parametrize("crash", [False, True])
def test_two_workers_own_separate_models_and_cleanup(tmp_path, monkeypatch, crash):
    path = deployment_config(tmp_path, monkeypatch, crash=crash)
    loaded = load_application_config(path)
    ExecutionOptions.from_params(loaded.params).validate_tools(loaded.tools)
    result = asyncio.run(
        run_experiment(
            ExperimentRequest(config=path, output_dir=tmp_path / "runs", run_id="test")
        )
    )
    assert result["num_errors"] == (4 if crash else 0)
    records = [json.loads(p.read_text()) for p in tmp_path.glob("xiaomi-*.json")]
    qwen = [json.loads(p.read_text()) for p in tmp_path.glob("qwen-*.json")]
    assert {r["cuda"] for r in records} == {"10", "13"}
    assert {r["cuda"] for r in qwen} == {"11,12", "14,15"}
    if not crash:
        assert len(records) == len(qwen) == 2
        assert len({r["port"] for r in records + qwen}) == 4
        outputs = [
            json.loads(p.read_text())
            for p in (tmp_path / "runs/test/test").glob("episode_*.json")
        ]
        bindings = [r["result"]["context"]["node_records"]['["test"]']["echo"]["values"]["deployments"] for r in outputs]
        assert {b["xiaomi"]["cuda"] for b in bindings} == {"10,11,12", "13,14,15"}
        assert all(b["xiaomi"]["endpoint"] != b["qwen"]["endpoint"] for b in bindings)
    # Both a normal exit and os._exit in the Worker must close model listeners.
    import socket, time

    for record in records + qwen:
        deadline = time.monotonic() + 3
        while True:
            try:
                with socket.create_connection(
                    ("127.0.0.1", record["port"]), timeout=0.1
                ):
                    pass
            except OSError:
                break
            if time.monotonic() > deadline:
                pytest.fail("orphan model server still listening")
            time.sleep(0.02)


def test_startup_failure_reaps_process(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "4")
    config = ToolDeploymentConfig(
        backend="command",
        gpu_ids=[0],
        startup_timeout_s=0.2,
        shutdown_timeout_s=0.2,
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
    )
    service = ModelService(config, tmp_path / "model")

    async def scenario():
        with pytest.raises(TimeoutError):
            await service.start()
        assert service.process is None
        assert service.lease is None

    asyncio.run(scenario())


def test_invalid_tool_gpu_rejected_before_start(tmp_path, monkeypatch):
    path = deployment_config(tmp_path, monkeypatch)
    value = json.loads(path.read_text())
    value["tools"][0]["deployment"]["gpu_ids"] = [3]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="GPU index"):
        asyncio.run(
            run_experiment(ExperimentRequest(config=path, output_dir=tmp_path / "runs"))
        )
    assert not list(tmp_path.glob("xiaomi-*.json"))


def test_cancel_during_model_startup_cleans_process(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    config = ToolDeploymentConfig(
        backend="command",
        gpu_ids=[0],
        shutdown_timeout_s=0.2,
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
    )
    service = ModelService(config, tmp_path / "model")

    async def scenario():
        task = asyncio.create_task(service.start())
        while service.process is None:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.process is None
        assert service.lease is None

    asyncio.run(scenario())
