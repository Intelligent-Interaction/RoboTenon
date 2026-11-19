"""Xiaomi Robotics 1 native RoboCasa365 and VLABench batch policy.

Uses the released HF processor/model and the vendor evaluator's image/state
preprocessing. Model and simulator may run in separate Python environments.
"""

import time
from pathlib import Path

import numpy as np

from .preprocessing import (
    CAMERA_KEYS,
    build_messages,
    build_vlabench_messages,
    observation_to_state,
    vlabench_observation_to_state,
)

OBSERVATION_PARAMS = {"camera_size": 256}
RNG_MODES = frozenset({"official_stream", "session_deterministic"})
PROTOCOLS = {
    "robocasa365": {"history": 4, "replan": 16, "robot_type": "robocasa365", "action_dim": 12},
    "vlabench": {"history": 1, "replan": 5, "robot_type": "vlabench_choice", "action_dim": 7},
}


def vlabench_absolute_action_chunk(actions, execution_states):
    """Match Xiaomi's evaluator: freeze five cumulative EE targets per plan."""
    actions = np.asarray(actions, dtype=np.float32)
    execution_states = np.asarray(execution_states, dtype=np.float32)
    if actions.ndim != 3 or actions.shape[2] < 7:
        raise ValueError(f"invalid VLABench action shape: {actions.shape}")
    if execution_states.shape != (actions.shape[0], 6):
        raise ValueError(
            "VLABench execution states must have shape "
            f"({actions.shape[0]}, 6), got {execution_states.shape}"
        )
    absolute = actions.copy()
    absolute[:, :, :6] = (
        execution_states[:, None, :] + np.cumsum(actions[:, :, :6], axis=1)
    )
    absolute[:, :, 3:6] = (
        absolute[:, :, 3:6] + np.pi
    ) % (2.0 * np.pi) - np.pi
    return absolute


class XiaomiBatchVLA:
    def __init__(
        self,
        model_path,
        *,
        crop_ratio=0.95,
        vlabench_image_size=480,
        vlabench_cot=False,
        vlabench_position_offset=(0.0, -0.4, 0.78),
        vlabench_gripper_threshold=0.2,
        inference_seed=42,
        rng_mode="session_deterministic",
        metrics_dir=None,
        warmup_observations=None,
        warmup_batch_size=4,
    ):
        import torch
        from transformers import AutoModel, AutoProcessor

        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(
            model_path, trust_remote_code=True, use_fast=False
        )
        self.processor.tokenizer.padding_side = "left"
        self.model = (
            AutoModel.from_pretrained(
                model_path,
                trust_remote_code=True,
                attn_implementation="flash_attention_2",
                dtype=torch.bfloat16,
            )
            .cuda()
            # Xiaomi's server casts the entire model, including RoPE buffers.
            # from_pretrained(dtype=...) only casts weights; leaving inv_freq
            # in FP32 changes actions even with identical inputs and RNG state.
            .to(torch.bfloat16)
            .eval()
        )
        self.crop_ratio = crop_ratio
        self.vlabench_image_size = int(vlabench_image_size)
        self.vlabench_cot = bool(vlabench_cot)
        self.vlabench_position_offset = np.asarray(vlabench_position_offset, dtype=np.float32)
        self.vlabench_gripper_threshold = float(vlabench_gripper_threshold)
        if self.vlabench_image_size < 1 or self.vlabench_position_offset.shape != (3,):
            raise ValueError("invalid VLABench image size or position offset")
        if rng_mode not in RNG_MODES:
            raise ValueError(f"rng_mode must be one of {sorted(RNG_MODES)}")
        self.seed, self.rng_mode, self.metrics_dir = (
            int(inference_seed),
            rng_mode,
            metrics_dir,
        )
        self.sessions = {}
        self.forward_records = []
        self._noise_seeds = None
        if self.rng_mode == "session_deterministic":
            self._install_episode_noise()
        if warmup_observations:
            if self.rng_mode == "official_stream":
                # The Xiaomi evaluator sends the first real rollout directly to
                # the model. Keep optional compilation warmup from advancing the
                # model service's global random stream.
                device = self.model.device.index
                devices = [torch.cuda.current_device() if device is None else device]
                with torch.random.fork_rng(devices=devices):
                    self._warmup(warmup_observations, warmup_batch_size)
            else:
                self._warmup(warmup_observations, warmup_batch_size)

    def _warmup(self, path, batch_size):
        with np.load(path, allow_pickle=False) as data:
            for size in (1, batch_size):
                requests = []
                for i in range(size):
                    index = i % 2
                    obs = {
                        group: {
                            key.split("/", 2)[2]: data[key]
                            for key in data.files
                            if key.startswith(f"{index}/{group}/")
                        }
                        for group in ("images", "state")
                    }
                    obs["task_description"] = str(data[f"{index}/task_description"])
                    session = f"warmup-{i}"
                    self.open_session(
                        session,
                        {
                            "benchmark_id": "robocasa365",
                            "task": {"name": str(data[f"{index}/name"])},
                        },
                    )
                    requests.append(
                        {
                            "session_id": session,
                            "request_id": session,
                            "observation": {
                                "history": [obs] * PROTOCOLS["robocasa365"]["history"],
                                "task_description": obs["task_description"],
                            },
                        }
                    )
                self.act_batch(requests)
                for request in requests:
                    self.close_session(request["session_id"], {})
        self.forward_records.clear()

    def _install_episode_noise(self):
        """Inject only this model's initial noise, with no global torch patch.

        The released forward has one torch.randn_like(action_mask) call and no
        public generator argument. Clone that function's globals so unrelated
        models and sessions never share or overwrite its random stream.
        """
        import ast
        import inspect
        import textwrap
        from functools import wraps
        from types import FunctionType, MethodType

        torch = self.torch
        original = inspect.unwrap(self.model.forward)
        tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
        draws = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "torch"
            and n.func.attr in {"randn_like", "randn", "rand", "rand_like"}
        ]
        if len(draws) != 1 or draws[0].func.attr != "randn_like":
            raise ValueError(
                "Xiaomi forward random sampling changed; review deterministic batch adapter"
            )
        owner = self

        class TorchView:
            def __getattr__(self, name):
                return getattr(torch, name)

            def randn_like(self, tensor):
                if (
                    owner._noise_seeds is None
                    or len(owner._noise_seeds) != tensor.shape[0]
                ):
                    raise RuntimeError("inference noise/session batch mismatch")
                return torch.stack(
                    [
                        torch.randn(
                            tensor.shape[1:],
                            dtype=tensor.dtype,
                            device=tensor.device,
                            generator=torch.Generator(device=tensor.device).manual_seed(
                                seed
                            ),
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
        self.model.forward = MethodType(
            torch.no_grad()(wraps(original)(clone)), self.model
        )

    def open_session(self, session_id, task_info):
        import hashlib

        task = task_info["task"]
        benchmark = str(task_info.get("benchmark_id", "robocasa365")).removesuffix("_benchmark")
        if benchmark not in PROTOCOLS:
            raise ValueError(f"Xiaomi backend does not support {benchmark}")
        identity = f"{benchmark}:{task.get('name')}:{task.get('episode_idx', 0)}:{self.seed}"
        seed = (
            int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4], "big")
            if self.rng_mode == "session_deterministic"
            else None
        )
        self.sessions[session_id] = {
            "benchmark": benchmark,
            "query": 0,
            "seed": seed,
        }

    def act_batch(self, requests):
        if not requests:
            return []
        if any(not r["observation"].get("history") for r in requests):
            raise ValueError("Xiaomi batch requests require an observation history")
        chunks = self._infer(requests)
        return [
            {
                "request_id": request["request_id"],
                "action": {
                    "native_actions": [
                        {
                            "actions": action,
                            **(
                                {"control_mode": "ee_absolute"}
                                if self.sessions[request["session_id"]]["benchmark"]
                                == "vlabench"
                                else {}
                            ),
                        }
                        for action in chunk
                    ]
                },
            }
            for request, chunk in zip(requests, chunks)
        ]

    def _infer(self, requests):
        torch = self.torch
        started = time.perf_counter()
        benchmarks = {self.sessions[r["session_id"]]["benchmark"] for r in requests}
        if len(benchmarks) != 1:
            raise ValueError("a Xiaomi inference batch cannot mix benchmark protocols")
        benchmark = benchmarks.pop()
        protocol = PROTOCOLS[benchmark]
        messages, states, seeds = [], [], []
        vlabench_execution_states = []
        for request in requests:
            session = self.sessions[request["session_id"]]
            selected = request["observation"]["history"]
            if len(selected) != protocol["history"]:
                raise ValueError(
                    f"expected {protocol['history']} {benchmark} history frames, got {len(selected)}"
                )
            if benchmark == "robocasa365":
                images = {
                    key: np.stack([obs["images"][key] for obs in selected])
                    for key in CAMERA_KEYS
                }
                messages.append(
                    build_messages(
                        images,
                        request["observation"]["task_description"],
                        self.crop_ratio,
                    )
                )
                state = np.zeros((protocol["history"], 60), dtype=np.float32)
                state[:, :14] = np.stack(
                    [observation_to_state(obs["state"]) for obs in selected]
                )
            else:
                observation = selected[-1]
                messages.append(
                    build_vlabench_messages(
                        observation["images"],
                        request["observation"]["task_description"],
                        image_size=self.vlabench_image_size,
                        cot=self.vlabench_cot,
                    )
                )
                state = np.zeros((1, 60), dtype=np.float32)
                model_state = vlabench_observation_to_state(
                    observation["state"], self.vlabench_position_offset
                )
                state[0, :7] = model_state
                execution_state = model_state[:6].copy()
                execution_state[:3] += self.vlabench_position_offset
                vlabench_execution_states.append(execution_state)
            states.append(state)
            if self.rng_mode == "session_deterministic":
                seeds.append((session["seed"] + session["query"]) % (2**32))
        processor_args = {
            "tokenize": True,
            "return_dict": True,
            "return_tensors": "pt",
            "padding": True,
            "state": np.stack(states),
            "robot_type": protocol["robot_type"],
        }
        if benchmark == "robocasa365":
            processor_args["do_resize"] = False
        inputs = self.processor.apply_chat_template(messages, **processor_args)
        inputs["action_mask"] = (
            inputs["action_mask"].expand(len(requests), -1, -1).contiguous()
        )
        data = {
            k: (
                v.to(device="cuda", dtype=self.model.dtype)
                if v.is_floating_point()
                else v.cuda()
            )
            if isinstance(v, torch.Tensor)
            else v
            for k, v in inputs.items()
        }
        # Xiaomi's released VLABench checkpoint requires the same explicit
        # request seed used by the reference evaluator (default: 42).  The
        # model saves/restores the surrounding RNG state internally.
        data["seed"] = self.seed
        if self.rng_mode == "session_deterministic":
            self._noise_seeds = seeds
        try:
            torch.cuda.synchronize()
            inference_started = time.perf_counter()
            with torch.inference_mode():
                normalized = self.model(**data).actions
                actions = (
                    self.processor.decode_action(
                        normalized, robot_type=protocol["robot_type"]
                    )[:, :, : protocol["action_dim"]]
                    .float()
                    .cpu()
                    .numpy()
                )
                if benchmark == "vlabench":
                    actions[:, :, 6] = (
                        actions[:, :, 6] >= self.vlabench_gripper_threshold
                    ).astype(np.float32)
                    actions = vlabench_absolute_action_chunk(
                        actions, np.stack(vlabench_execution_states)
                    )
            inference_sec = time.perf_counter() - inference_started
        finally:
            if self.rng_mode == "session_deterministic":
                self._noise_seeds = None
        if (
            actions.shape[0] != len(requests)
            or actions.shape[1] < protocol["replan"]
            or not np.all(np.isfinite(actions))
        ):
            raise ValueError("invalid Xiaomi action chunk")
        self.forward_records.append(
            {
                "batch_size": len(requests),
                "benchmark": benchmark,
                "inference_sec": inference_sec,
                "total_sec": time.perf_counter() - started,
                "rng_mode": self.rng_mode,
            }
        )
        for request in requests:
            self.sessions[request["session_id"]]["query"] += 1
        return actions[:, : protocol["replan"]]

    def close_session(self, session_id, result):
        self.sessions.pop(session_id, None)

    def close(self):
        import json
        import os

        if self.metrics_dir:
            directory = Path(self.metrics_dir)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"xiaomi-{os.getpid()}.json").write_text(
                json.dumps(self.forward_records, indent=2)
            )
        self.sessions.clear()


def create_batch(**kwargs):
    return XiaomiBatchVLA(**kwargs)
