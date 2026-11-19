"""Batched Xiaomi Robotics 0 inference matching the official LIBERO client."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np


PROMPT = (
    "<|im_start|>user\n"
    "The following observations are captured from multiple views.\n"
    "# Base View\n"
    "<|vision_start|><|image_pad|><|vision_end|>\n"
    "# Left-Wrist View\n"
    "<|vision_start|><|image_pad|><|vision_end|>\n"
    "Generate robot actions for the task:\n"
    "{language} /no_cot<|im_end|>\n"
    "<|im_start|>assistant\n"
    "<cot></cot><|im_end|>\n"
)


def hash_data_to_seed(data, max_bytes=4):
    """Reproduce Xiaomi's stable per-observation inference seed."""
    from PIL import Image

    def encode(value):
        if isinstance(value, np.ndarray):
            return {
                "__type__": "numpy",
                "dtype": str(value.dtype),
                "shape": value.shape,
                "data": value.tobytes().hex(),
            }
        if isinstance(value, Image.Image):
            return {
                "__type__": "PIL.Image",
                "mode": value.mode,
                "size": value.size,
                "content_hash": hashlib.md5(value.tobytes()).hexdigest(),
            }
        if isinstance(value, set):
            return sorted(value)
        raise TypeError(f"Type {type(value)} is not JSON serializable")

    canonical = json.dumps(
        data,
        default=encode,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    seed = int(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), 16)
    return seed % (2 ** (8 * max_bytes)) if max_bytes > 0 else seed


def build_model_inputs(request):
    """Convert one RoboTenon LIBERO request into Xiaomi's official fields."""
    from PIL import Image

    history = request["observation"].get("history")
    if not history or len(history) != 1:
        raise ValueError("Xiaomi Robotics 0 requires exactly one observation frame")
    observation = history[-1]
    images = observation.get("images", {})
    pil_images = {}
    for source, target in (("agentview", "base"), ("wrist", "wrist_left")):
        if source not in images:
            raise ValueError(f"Xiaomi Robotics 0 observation is missing {source!r}")
        image = np.asarray(images[source])
        if image.shape != (256, 256, 3) or image.dtype != np.uint8:
            raise ValueError(
                f"{source} image must be uint8 HWC (256, 256, 3), got "
                f"{image.dtype} {image.shape}"
            )
        pil_images[target] = Image.fromarray(np.ascontiguousarray(image))

    proprio = np.asarray(observation.get("states"), dtype=np.float64)
    if proprio.shape != (8,) or not np.all(np.isfinite(proprio)):
        raise ValueError(f"LIBERO state must be finite shape (8,), got {proprio.shape}")
    # The official numpy concatenate retains float64 because its simulator
    # proprioception is float64 and the 24 padding values are Python floats.
    state = np.concatenate([proprio, np.zeros(24)])
    language = str(request["observation"].get("task_description", "")).capitalize()
    fields = {
        "task_id": "libero_all",
        "state": state,
        "base": pil_images["base"],
        "wrist_left": pil_images["wrist_left"],
        "language": language,
    }
    seed = hash_data_to_seed(fields)
    return fields, seed


class XiaomiRobotics0BatchVLA:
    def __init__(self, model_path, *, device="cuda", metrics_dir=None):
        import torch
        from transformers import AutoModel, AutoProcessor

        model_path = str(Path(model_path).expanduser().resolve())
        if not Path(model_path).is_dir():
            raise FileNotFoundError(f"checkpoint directory not found: {model_path}")
        self.torch = torch
        self.device = torch.device(device)
        self.processor = AutoProcessor.from_pretrained(
            model_path, trust_remote_code=True, use_fast=False
        )
        # Batched rows are left padded so each sample's final meaningful token
        # remains aligned with the single-item reference server.
        self.processor.tokenizer.padding_side = "left"
        self.model = (
            AutoModel.from_pretrained(
                model_path,
                trust_remote_code=True,
                attn_implementation="flash_attention_2",
                dtype=torch.bfloat16,
            )
            .to(self.device)
            .to(torch.bfloat16)
            .eval()
        )
        self.sessions = set()
        self.metrics_dir = Path(metrics_dir) if metrics_dir else None
        self.forward_records = []
        self._noise_seeds = None
        self._install_per_request_noise()

    def _install_per_request_noise(self):
        """Batch official per-request seeds without changing checkpoint code."""
        import ast
        import inspect
        import textwrap
        from functools import wraps
        from types import FunctionType, MethodType

        torch = self.torch
        original = inspect.unwrap(self.model.forward)
        tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
        draws = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "torch"
            and node.func.attr in {"randn_like", "randn", "rand", "rand_like"}
        ]
        if len(draws) != 1 or draws[0].func.attr != "randn_like":
            raise ValueError(
                "Xiaomi Robotics 0 sampling changed; review deterministic batch adapter"
            )
        owner = self

        class TorchView:
            def __getattr__(self, name):
                return getattr(torch, name)

            def manual_seed(self, seed):
                # randn_like below uses isolated generators, so the scalar seed
                # passed through the batched model call must not alter global RNG.
                return torch.Generator()

            def randn_like(self, tensor):
                if owner._noise_seeds is None or len(owner._noise_seeds) != tensor.shape[0]:
                    raise RuntimeError("inference noise/request batch mismatch")
                return torch.stack(
                    [
                        torch.randn(
                            tensor.shape[1:],
                            dtype=tensor.dtype,
                            device=tensor.device,
                            generator=torch.Generator(device=tensor.device).manual_seed(seed),
                        )
                        for seed in owner._noise_seeds
                    ]
                )

        clone = FunctionType(
            original.__code__,
            {**original.__globals__, "torch": TorchView()},
            original.__name__,
            original.__defaults__,
            original.__closure__,
        )
        clone.__kwdefaults__ = original.__kwdefaults__
        self.model.forward = MethodType(torch.no_grad()(wraps(original)(clone)), self.model)

    def open_session(self, session_id, task_info):
        benchmark = str(task_info.get("benchmark_id", "libero_benchmark"))
        supported_benchmarks = {
            "libero_benchmark",
            "libero_plus_benchmark",
            "libero_pro_benchmark",
        }
        if benchmark not in supported_benchmarks:
            raise ValueError(
                "Xiaomi Robotics 0 supports LIBERO, LIBERO-Plus, and LIBERO-Pro "
                f"only, got {benchmark}"
            )
        self.sessions.add(session_id)

    def act_batch(self, requests):
        if not requests:
            return []
        unknown = [request["session_id"] for request in requests if request["session_id"] not in self.sessions]
        if unknown:
            raise ValueError(f"request references unopened sessions: {unknown}")

        torch = self.torch
        started = time.perf_counter()
        converted = [build_model_inputs(request) for request in requests]
        fields = [item[0] for item in converted]
        seeds = [item[1] for item in converted]
        prompts = [PROMPT.format(language=item["language"] + ".") for item in fields]
        images = [image for item in fields for image in (item["base"], item["wrist_left"])]
        inputs = self.processor(
            text=prompts,
            images=images,
            videos=None,
            padding=True,
            return_tensors="pt",
        )
        inputs = {
            key: value.to(self.device) if isinstance(value, torch.Tensor) else value
            for key, value in inputs.items()
        }
        inputs["state"] = torch.from_numpy(np.stack([item["state"] for item in fields])).to(
            device=self.device, dtype=self.model.dtype
        )[:, None]
        inputs["action_mask"] = self.processor.get_action_mask(
            "libero_all", batch_size=len(requests)
        ).to(device=self.device, dtype=self.model.dtype)
        # The patched sampling path consumes the official seed for every row.
        inputs["seed"] = 0
        self._noise_seeds = seeds
        try:
            with torch.inference_mode():
                normalized = self.model(**inputs).actions
                actions = self.processor.decode_action(
                    normalized, robot_type="libero_all"
                )[:, :10, :7].float().cpu().numpy()
        finally:
            self._noise_seeds = None

        if actions.shape != (len(requests), 10, 7) or not np.all(np.isfinite(actions)):
            raise ValueError(
                f"invalid Xiaomi Robotics 0 action chunk {actions.shape}; expected "
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
                        {"actions": action.tolist()} for action in chunk
                    ]
                },
            }
            for request, chunk in zip(requests, actions, strict=True)
        ]

    def close_session(self, session_id, result):
        self.sessions.discard(session_id)

    def close(self):
        if self.metrics_dir:
            import os

            self.metrics_dir.mkdir(parents=True, exist_ok=True)
            output = self.metrics_dir / f"xiaomi-robotics0-{os.getpid()}-forward-times.jsonl"
            with output.open("a", encoding="utf-8") as stream:
                for record in self.forward_records:
                    stream.write(json.dumps(record) + "\n")
        self.forward_records.clear()


def create_batch(**kwargs):
    return XiaomiRobotics0BatchVLA(**kwargs)
