"""Deterministic providers exercised through actual model/Graph processes."""

import os
import time

from robotenon.modules.base import BaseModule
from robotenon.runtime.registry import register_module


class EchoProvider:
    def __init__(self, delay=0, startup_delay=0):
        time.sleep(startup_delay)
        self.sessions = {}
        self.delay = delay
        self.created_at = time.monotonic()

    def open_session(self, session_id, metadata):
        assert session_id not in self.sessions
        self.sessions[session_id] = 0

    def infer_batch(self, requests):
        time.sleep(self.delay)
        out = []
        for request in requests:
            key = request["session_id"]
            self.sessions[key] += 1
            out.append(
                {
                    "request_id": request["request_id"],
                    "output": {
                        "value": request["observation"],
                        "count": self.sessions[key],
                        "batch_size": len(requests),
                        "pid": os.getpid(),
                        "device": os.environ.get("CUDA_VISIBLE_DEVICES"),
                        "virtual_device": os.environ.get("ROBOTENON_VIRTUAL_GPU_ID"),
                        "created_at": self.created_at,
                    },
                }
            )
        return list(reversed(out))

    def close_session(self, session_id, result):
        del self.sessions[session_id]

    def close(self):
        assert not self.sessions


@register_module("batch_test_policy", kind="agent")
class BatchTestPolicy(BaseModule):
    async def run(self, ctx):
        outputs = []
        for i in range(3):
            result = await ctx.call_tool("model", {"step": i})
            assert result.ok
            assert result.output["value"] == {"step": i}
            assert result.output["count"] == i + 1
            outputs.append(result.output)
        return {"success": True, "outputs": outputs}


class BatchVLA:
    def __init__(self):
        self.sessions = {}

    def open_session(self, session_id, info):
        self.sessions[session_id] = info

    def act_batch(self, requests):
        import numpy as np

        for request in requests:
            assert request["session_id"] in self.sessions
            assert request["observation"]["images"]["front"].dtype == np.uint8
        return [
            {"request_id": r["request_id"], "action": {"actions": [1.0]}}
            for r in reversed(requests)
        ]

    def close_session(self, session_id, result):
        self.sessions.pop(session_id)

    def close(self):
        assert not self.sessions


@register_module("virtual_gpu_probe", kind="agent")
class VirtualGPUProbe(BaseModule):
    async def run(self, ctx):
        assert os.environ['ROBOTENON_VIRTUAL_GPU_ID'] in {'v0', 'v1'}
        assert os.environ['CUDA_VISIBLE_DEVICES'] == '0'
        assert os.environ['MUJOCO_EGL_DEVICE_ID'] == '0'
        return {'success': True}
