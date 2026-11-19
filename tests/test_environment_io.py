"""Environment IO contracts that are independent of benchmark-specific models."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from PIL import Image

from robotenon.envs.real_robot import RealRobotSession
from robotenon.schemas import CameraSpec, EnvironmentFeedback, EnvironmentIO, SessionSpec
from robotenon.tools.base import ToolContext
from robotenon.tools.qwen35_9b import Qwen35_9BTool, _frame_data_url


def test_qwen_accepts_custom_camera_channels_and_environment_orientation(monkeypatch):
    pixels = np.zeros((32, 32, 3), dtype=np.uint8)
    pixels[:16, :, 0] = 255
    pixels[16:, :, 2] = 255
    spec = EnvironmentIO(cameras={
        "front_custom": CameraSpec(role="external", flip_vertical=True),
        "hand_custom": CameraSpec(role="wrist", flip_vertical=False),
    })
    feedback = EnvironmentFeedback(session_id=uuid4(), io=spec)
    ctx = ToolContext(
        graph_context=SimpleNamespace(feedback=feedback),
        observation_view=SimpleNamespace(frames={
            "front_custom": pixels,
            "hand_custom": pixels,
        }),
    )
    tool = Qwen35_9BTool(dry_run=False)
    payload = []

    def request(body):
        payload.append(json.loads(body))
        return {"decision": "act"}

    monkeypatch.setattr(tool, "_request_json", request)
    result = asyncio.run(tool.call({"image_roles": ["external", "wrist"]}, ctx))
    assert result.metadata["image_input_keys"] == ["front_custom", "hand_custom"]
    content = payload[0]["messages"][1]["content"]
    assert content[-1]["type"] == "text"
    assert content[-1]["text"] == "Complete the task"
    assert content[0]["text"].startswith("Image 1:")
    images = [
        np.asarray(Image.open(io.BytesIO(base64.b64decode(item["image_url"]["url"].split(",")[1]))))
        for item in content if item["type"] == "image_url"
    ]
    assert images[0][0, 0, 2] > 240 and images[0][0, 0, 0] < 20
    assert images[1][0, 0, 0] > 240 and images[1][0, 0, 2] < 20
    with pytest.raises(ValueError, match="missing"):
        asyncio.run(tool.call({"image_keys": ["not_a_camera"]}, ctx))


def test_image_bytes_respect_declared_orientation():
    pixels = np.zeros((32, 32, 3), dtype=np.uint8)
    pixels[:16] = 255
    data = io.BytesIO()
    Image.fromarray(pixels).save(data, format="PNG")
    output = _frame_data_url(data.getvalue(), flip_vertical=True)
    decoded = np.asarray(Image.open(io.BytesIO(base64.b64decode(output.split(",")[1]))))
    assert decoded[0].mean() < 10 and decoded[-1].mean() > 245


def test_real_environment_preserves_io_from_endpoint():
    async def source(*args, **kwargs):
        return {
            "io": {
                "cameras": {"custom": {"role": "wrist"}},
                "action": {"format": "custom_native", "dimensions": 7},
            },
            "state": {},
        }

    session = RealRobotSession(
        SessionSpec(task_id="t", environment_id="real"),
        robot_name="r",
        observation_source=SimpleNamespace(reset=source, observe=source),
    )
    feedback = asyncio.run(session.reset(session.spec))
    assert feedback.io.action.dimensions == 7
    assert not feedback.io.cameras["custom"].flip_vertical
    restored = EnvironmentFeedback.model_validate(feedback.model_dump(mode="json"))
    assert restored.io == feedback.io
