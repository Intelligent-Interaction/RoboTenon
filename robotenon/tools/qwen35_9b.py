"""Local Qwen3.5-VL high-level Tool served through Transformers' API.

The checkpoint used by the server is the Qwen3.5 9B vision-language model.
The historical ``Qwen3.5-9B`` registration remains as a compatibility alias;
the RoboCasa365 graph addresses it as ``Qwen3.5-VL``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolContext, ToolResult
from .registry import register_tool


def _extract_json(text: str) -> dict[str, Any]:
    """Extract one JSON object from plain JSON, markdown, or think output."""

    cleaned = re.sub(
        r"<think>.*?</think>",
        "",
        text.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", cleaned.strip(), flags=re.IGNORECASE
    )
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        # Only decode the first object. Searching later opening braces can
        # silently accept a nested object inside an incomplete outer result.
        start = cleaned.find("{")
        if start < 0:
            raise ValueError("Qwen3.5-VL response did not contain JSON")
        try:
            value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        except json.JSONDecodeError as exc:
            raise ValueError("Qwen3.5-VL response contained incomplete or invalid JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("Qwen3.5-VL response must be one JSON object")
    return value


def _frame_data_url(value: Any, *, flip_vertical: bool = False) -> str | None:
    if isinstance(value, (bytes, bytearray, memoryview)) and not flip_vertical:
        return "data:image/jpeg;base64," + base64.b64encode(bytes(value)).decode("ascii")
    try:
        import numpy as np
        from PIL import Image

        array = np.asarray(Image.open(io.BytesIO(bytes(value))).convert("RGB")) if isinstance(value, (bytes, bytearray, memoryview)) else np.asarray(value)
        if array.ndim != 3 or array.shape[-1] not in (3, 4):
            return None
        if array.dtype != np.uint8:
            array = np.clip(
                array * (255.0 if float(array.max()) <= 1.0 else 1.0), 0, 255
            ).astype(np.uint8)
        if flip_vertical:
            array = np.ascontiguousarray(np.flipud(array))
        buffer = io.BytesIO()
        Image.fromarray(array[..., :3]).save(buffer, format="JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except (ImportError, TypeError, ValueError):
        return None


@register_tool(
    "Qwen3.5-9B",
    version="1.0.0",
    kind="model",
    description="Local Qwen3.5-9B high-level planner over a localhost OpenAI-compatible endpoint.",
)
class Qwen35_9BTool(BaseTool):
    """Call the resident local Qwen3.5-VL server and return JSON."""

    def __init__(
        self,
        *,
        model: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 180.0,
        max_completion_tokens: int = 1024,
        temperature: float = 0.0,
        max_response_retries: int = 2,
        dry_run: bool | None = None,
    ) -> None:
        configured_model = model or os.environ.get("QWEN_MODEL") or os.environ.get(
            "QWEN_MODEL_PATH"
        )
        if configured_model:
            self.model = configured_model
        else:
            local_model = Path("/root/models/Qwen3.5-9B")
            self.model = (
                str(local_model)
                if local_model.is_dir()
                else "Qwen/Qwen3.5-9B"
            )
        self.endpoint = endpoint or os.environ.get(
            "QWEN_ENDPOINT", "http://127.0.0.1:8000/v1/chat/completions"
        )
        self.api_key = (api_key or os.environ.get("QWEN_API_KEY") or "EMPTY").strip()
        self.timeout_s = timeout_s
        self.max_completion_tokens = max_completion_tokens
        self.temperature = temperature
        if max_response_retries < 0 or max_completion_tokens < 1:
            raise ValueError("Response retries must be nonnegative and token budget positive")
        self.max_response_retries = max_response_retries
        env_dry = os.environ.get("QWEN_DRY_RUN")
        self.dry_run = (
            str(env_dry).lower() in {"1", "true", "yes", "on"}
            if dry_run is None and env_dry is not None
            else bool(dry_run)
            if dry_run is not None
            else False
        )
        self._started = False

    def bind_deployment(self, host, port, model_path):
        self.endpoint = f"http://{host}:{port}/v1/chat/completions"
        if model_path:
            self.model = model_path

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        self._started = False

    async def call(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if not self._started:
            await self.start()
        task = str(arguments.get("task") or "Complete the task")
        prompt = str(arguments.get("prompt") or task)
        system_prompt = str(
            arguments.get("system_prompt")
            or (
                "You are the high-level vision-language controller for an embodied "
                "robot. Follow the JSON schema in the user prompt and return exactly "
                "one JSON object."
            )
        )
        if self.dry_run:
            return ToolResult(
                output={
                    "decision": "act",
                    "subgoal": str(arguments.get("subgoal") or task),
                    "completed": [],
                    "remaining": [task],
                    "reason": "dry_run",
                },
                metadata={"backend": "dry_run", "model": self.model},
            )

        # Put visual evidence before the long controller prompt.  This follows
        # the checkpoint's native multimodal ordering and reduces the chance
        # that a long textual history anchors generation before images are read.
        content: list[dict[str, Any]] = []
        image_input_keys: list[str] = []
        image_input_shapes: dict[str, list[int]] = {}
        supplied_images = arguments.get("image_inputs")
        if supplied_images is not None:
            if not isinstance(supplied_images, (list, tuple)):
                raise ValueError("image_inputs must be a list")
            for index, item in enumerate(supplied_images):
                if not isinstance(item, Mapping):
                    raise TypeError("each image_inputs item must be an object")
                key = str(item.get("camera") or f"image_{index + 1}")
                role = str(item.get("role") or "camera")
                observation_id = str(item.get("observation_id") or "")
                data_url = item.get("data_url")
                if not isinstance(data_url, str) or not data_url.startswith(
                    "data:image/"
                ):
                    raise ValueError(
                        "image_inputs data_url must be an inline image data URL"
                    )
                label = f"Image {index + 1}: {key} ({role})"
                if observation_id:
                    label += f" observation_id={observation_id}"
                content.append({"type": "text", "text": label})
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    }
                )
                image_input_keys.append(key)
        if bool(arguments.get("use_context_images", supplied_images is None)):
            view = context.observation
            frames = getattr(view, "frames", {}) if view is not None else {}
            camera_specs = context.environment_io.cameras
            requested_keys = arguments.get("image_keys")
            if isinstance(requested_keys, (list, tuple)):
                image_keys = [str(key) for key in requested_keys]
                missing = [key for key in image_keys if key not in frames]
                if missing:
                    raise ValueError(
                        "requested camera channels are missing: " + ", ".join(missing)
                    )
            else:
                roles = arguments.get("image_roles") or []
                image_keys = [
                    key
                    for role in roles
                    for key, spec in camera_specs.items()
                    if spec.role == role and key in frames
                ]
                if not image_keys:
                    image_keys = [key for key in camera_specs if key in frames]
                if not image_keys:
                    image_keys = [
                        key
                        for key, frame in frames.items()
                        if len(getattr(frame, "shape", ())) == 3
                    ]
            decoded_context_images = 0
            for key in image_keys:
                data_url = (
                    _frame_data_url(
                        frames.get(key),
                        flip_vertical=bool(
                            camera_specs.get(key) and camera_specs[key].flip_vertical
                        ),
                    )
                    if isinstance(frames, Mapping)
                    else None
                )
                if data_url is None:
                    continue
                image_number = len(image_input_keys) + 1
                content.append(
                    {
                        "type": "text",
                        "text": (
                            f"Image {image_number}: {key} "
                            f"({camera_specs[key].role if key in camera_specs else 'camera'})"
                        ),
                    }
                )
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    }
                )
                image_input_keys.append(key)
                decoded_context_images += 1
                value = frames.get(key)
                shape = getattr(value, "shape", None)
                if shape is not None:
                    image_input_shapes[key] = [int(item) for item in shape]
            if image_keys and not decoded_context_images:
                raise RuntimeError(
                    "Qwen3.5-VL received no decodable image frames for requested keys: "
                    + ", ".join(image_keys)
                )
        content.append({"type": "text", "text": prompt})
        requested_token_budget = arguments.get(
            "max_completion_tokens", self.max_completion_tokens
        )
        if isinstance(requested_token_budget, bool):
            raise ValueError("max_completion_tokens must be a positive integer")
        try:
            requested_token_budget = int(requested_token_budget)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "max_completion_tokens must be a positive integer"
            ) from exc
        if requested_token_budget < 1:
            raise ValueError("max_completion_tokens must be a positive integer")
        requested_token_budget = min(
            requested_token_budget, self.max_completion_tokens
        )
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": requested_token_budget,
                "temperature": self.temperature,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {"role": "user", "content": content},
                ],
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode()
        result = await asyncio.to_thread(self._request_json, body)
        return ToolResult(
            output=result,
            metadata={
                "backend": "local_transformers",
                "model": self.model,
                "endpoint": self.endpoint,
                "image_input_keys": image_input_keys,
                "image_input_count": len(image_input_keys),
                "image_input_shapes": image_input_shapes,
                "requested_max_completion_tokens": requested_token_budget,
            },
        )

    def _request_json(self, body: bytes) -> dict[str, Any]:
        request_body = json.loads(body)
        last_error = "missing response"
        for _attempt in range(self.max_response_retries + 1):
            payload = self._request_payload(json.dumps(request_body).encode())
            try:
                choice = payload["choices"][0]
                # A truncated response can contain a valid nested object.
                # Never execute it as if it were a complete planner result.
                if choice.get("finish_reason") == "length":
                    last_error = "response exceeded completion token budget"
                    request_body["max_tokens"] = min(
                        int(request_body["max_tokens"]) * 2,
                        self.max_completion_tokens * 4,
                    )
                    continue
                message = choice["message"]
                content = message.get("content") or message.get("reasoning_content")
                if isinstance(content, list):
                    content = "".join(
                        str(item.get("text", ""))
                        for item in content
                        if isinstance(item, Mapping)
                    )
                return _extract_json(str(content))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = str(exc)
        raise RuntimeError(
            f"Invalid Qwen3.5-VL response after {self.max_response_retries + 1} attempts: {last_error}"
        )

    def _request_payload(self, body: bytes) -> dict[str, Any]:
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            host = urllib.parse.urlparse(self.endpoint).hostname
            opener = (
                urllib.request.build_opener(urllib.request.ProxyHandler({}))
                if host in {"127.0.0.1", "localhost", "::1"}
                else urllib.request
            )
            with opener.open(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            raise RuntimeError(
                f"Qwen3.5-VL local endpoint is unavailable at {self.endpoint}"
            ) from exc
        return payload


# The checkpoint is multimodal even though its historical local directory is
# named ``Qwen3.5-9B``. Register the direct model name used by the XR365 graph
# without duplicating the resident client implementation.
register_tool(
    "Qwen3.5-VL",
    Qwen35_9BTool,
    version="1.0.0",
    kind="model",
    description="Local Qwen3.5-VL high-level planner/verifier over an OpenAI-compatible endpoint.",
)


__all__ = ["Qwen35_9BTool"]
