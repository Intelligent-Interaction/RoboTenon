"""Batched LeRobot PI0.5 inference using checkpoint-owned processors."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np


IMAGE_KEYS = ("agentview", "wrist")
POLICY_IMAGE_KEYS = ("observation.images.image", "observation.images.image2")


def build_numpy_batch(requests):
    """Validate RoboTenon observations and build the LeRobot policy fields."""
    images = {key: [] for key in IMAGE_KEYS}
    states = []
    tasks = []
    for request in requests:
        history = request["observation"].get("history")
        if not history or len(history) != 1:
            raise ValueError("PI0.5 requires exactly one observation frame")
        observation = history[-1]
        for key in IMAGE_KEYS:
            if key not in observation.get("images", {}):
                raise ValueError(f"PI0.5 LIBERO observation is missing {key!r}")
            image = np.asarray(observation["images"][key])
            if image.shape != (256, 256, 3) or image.dtype != np.uint8:
                raise ValueError(
                    f"PI0.5 {key} image must be uint8 HWC (256, 256, 3), got "
                    f"{image.dtype} {image.shape}"
                )
            images[key].append(image)
        state = np.asarray(observation.get("states"), dtype=np.float32)
        if state.shape != (8,) or not np.all(np.isfinite(state)):
            raise ValueError(f"PI0.5 state must be finite shape (8,), got {state.shape}")
        states.append(state)
        tasks.append(str(request["observation"].get("task_description", "")))

    batch = {
        policy_key: np.transpose(np.stack(images[source_key]), (0, 3, 1, 2)).astype(
            np.float32
        )
        / 255.0
        for source_key, policy_key in zip(IMAGE_KEYS, POLICY_IMAGE_KEYS, strict=True)
    }
    batch["observation.state"] = np.stack(states)
    batch["task"] = tasks
    return batch


class PI05BatchVLA:
    def __init__(
        self,
        model_path,
        *,
        device="cuda",
        inference_seed=1000,
        tokenizer_path=None,
        metrics_dir=None,
    ):
        import torch
        from lerobot.policies.factory import make_pre_post_processors
        from lerobot.policies.pi05 import PI05Policy

        model_path = str(Path(model_path).expanduser().resolve())
        if not Path(model_path).is_dir():
            raise FileNotFoundError(f"PI0.5 checkpoint directory not found: {model_path}")
        self.torch = torch
        self.device = torch.device(device)
        torch.manual_seed(int(inference_seed))
        if self.device.type == "cuda":
            torch.cuda.manual_seed_all(int(inference_seed))
        self.policy = PI05Policy.from_pretrained(model_path).to(self.device).eval()
        preprocessor_overrides = {"device_processor": {"device": str(self.device)}}
        if tokenizer_path:
            tokenizer_path = str(Path(tokenizer_path).expanduser().resolve())
            if not Path(tokenizer_path).is_dir():
                raise FileNotFoundError(f"PI0.5 tokenizer directory not found: {tokenizer_path}")
            preprocessor_overrides["tokenizer_processor"] = {
                "tokenizer_name": tokenizer_path
            }
        self.preprocessor, self.postprocessor = make_pre_post_processors(
            self.policy.config,
            model_path,
            preprocessor_overrides=preprocessor_overrides,
        )
        self.sessions = set()
        self.metrics_dir = Path(metrics_dir) if metrics_dir else None
        self.forward_records = []

    def open_session(self, session_id, task_info):
        benchmark = str(task_info.get("benchmark_id", "libero_benchmark"))
        if benchmark != "libero_benchmark":
            raise ValueError(f"PI0.5 backend supports LIBERO only, got {benchmark}")
        self.sessions.add(session_id)

    def act_batch(self, requests):
        if not requests:
            return []
        unknown = [r["session_id"] for r in requests if r["session_id"] not in self.sessions]
        if unknown:
            raise ValueError(f"PI0.5 request references unopened sessions: {unknown}")

        started = time.perf_counter()
        numpy_batch = build_numpy_batch(requests)
        batch = {
            key: (
                self.torch.from_numpy(value)
                if isinstance(value, np.ndarray)
                else value
            )
            for key, value in numpy_batch.items()
        }
        batch = self.preprocessor(batch)
        with self.torch.inference_mode():
            actions = self.policy.predict_action_chunk(batch)[:, :10]
            actions = self.postprocessor(actions)
        if isinstance(actions, dict):
            actions = actions["action"]
        actions = actions.detach().float().cpu().numpy()
        if actions.shape != (len(requests), 10, 7) or not np.all(np.isfinite(actions)):
            raise ValueError(
                f"PI0.5 returned invalid action chunk {actions.shape}; expected "
                f"({len(requests)}, 10, 7)"
            )
        self.forward_records.append(
            {"batch_size": len(requests), "seconds": time.perf_counter() - started}
        )
        return [
            {
                "request_id": request["request_id"],
                "action": {
                    "native_actions": [
                        {"actions": action.tolist()} for action in action_chunk
                    ]
                },
            }
            for request, action_chunk in zip(requests, actions, strict=True)
        ]

    def close_session(self, session_id, result):
        self.sessions.discard(session_id)

    def close(self):
        if self.metrics_dir:
            self.metrics_dir.mkdir(parents=True, exist_ok=True)
            path = self.metrics_dir / "pi05-forward-times.jsonl"
            import json

            with path.open("a", encoding="utf-8") as stream:
                for record in self.forward_records:
                    stream.write(json.dumps(record) + "\n")
        self.forward_records.clear()
