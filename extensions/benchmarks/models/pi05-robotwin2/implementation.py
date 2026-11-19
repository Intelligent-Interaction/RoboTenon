"""Batched OpenPI pi0.5 inference for the RoboTwin 2.0 checkpoint."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def _patch_openpi_singleton_resize() -> None:
    """Preserve a real batch dimension of one in older OpenPI checkouts."""
    from openpi.shared import image_tools

    resize = image_tools.resize_with_pad_torch
    if getattr(resize, "_robotenon_preserves_batch", False):
        return

    def resize_with_batch_preserved(images, *args, **kwargs):
        was_batched = images.dim() == 4
        result = resize(images, *args, **kwargs)
        if was_batched and result.dim() == 3:
            result = result.unsqueeze(0)
        return result

    resize_with_batch_preserved._robotenon_preserves_batch = True
    image_tools.resize_with_pad_torch = resize_with_batch_preserved


class PI05RoboTwin2VLA:
    def __init__(
        self,
        model_path,
        tokenizer_path,
        device="cuda",
        inference_steps=10,
        action_chunk=8,
    ):
        import safetensors.torch
        import sentencepiece
        import torch
        from openpi.models import model as model_types
        from openpi.models.pi0_config import Pi0Config
        from openpi.models_pytorch.pi0_pytorch import PI0Pytorch

        _patch_openpi_singleton_resize()
        self.torch = torch
        self.device = torch.device(device)
        self.action_chunk = int(action_chunk)
        self.inference_steps = int(inference_steps)
        model_path = Path(model_path)
        config = Pi0Config(pi05=True, action_horizon=32, pytorch_compile_mode=None)
        with torch.device("meta"):
            self.model = PI0Pytorch(config)
        self.model.to_empty(device=self.device)
        paligemma = self.model.paligemma_with_expert.paligemma
        paligemma.model.language_model.embed_tokens.weight = paligemma.lm_head.weight
        safetensors.torch.load_model(
            self.model,
            model_path / "model.safetensors",
            device=str(self.device),
        )
        self.model.paligemma_with_expert.to_bfloat16_for_selected_params("bfloat16")
        self.model.eval()
        self.model_types = model_types
        stats_path = model_path / "assets/pi0.5_clean_randomize_joint_training/norm_stats.json"
        raw = json.loads(stats_path.read_text())["norm_stats"]
        self.stats = {
            key: {name: np.asarray(value, dtype=np.float32) for name, value in values.items()}
            for key, values in raw.items()
        }
        self.tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(tokenizer_path))
        self.sessions = set()

    def open_session(self, session_id, task_info):
        self.sessions.add(session_id)

    def _tokens(self, prompt, state):
        state_bins = np.digitize(state, bins=np.linspace(-1, 1, 257)[:-1]) - 1
        text = (
            f"Task: {str(prompt).strip().replace('_', ' ')}, "
            f"State: {' '.join(map(str, state_bins))};\nAction: "
        )
        tokens = self.tokenizer.encode(text, add_bos=True)[:200]
        mask = [True] * len(tokens)
        tokens += [0] * (200 - len(tokens))
        mask += [False] * (200 - len(mask))
        return np.asarray(tokens, np.int64), np.asarray(mask, bool)

    def _prepare(self, request):
        obs = request["observation"]["history"][-1]
        state = np.asarray(obs["joint_state"], dtype=np.float32).copy()
        state *= np.array(
            [1, -1, -1, 1, 1, 1, 1, 1, -1, -1, 1, 1, 1, 1],
            np.float32,
        )
        linear = state[[6, 13]] * (0.05800 - 0.01844) + 0.01844
        angular = np.arcsin(
            np.clip((0.022**2 + linear**2 - 0.036**2) / (2 * 0.022 * linear), -1, 1)
        )
        state[[6, 13]] = (angular - 0.5476) / (1.6296 - 0.5476)
        state_stats = self.stats["state"]
        state = (state - state_stats["mean"]) / (state_stats["std"] + 1e-6)
        data = {
            "image": {
                "base_0_rgb": np.asarray(obs["images"]["head_camera"], dtype=np.uint8),
                "left_wrist_0_rgb": np.asarray(obs["images"]["left_camera"], dtype=np.uint8),
                "right_wrist_0_rgb": np.asarray(obs["images"]["right_camera"], dtype=np.uint8),
            },
            "image_mask": {
                "base_0_rgb": True,
                "left_wrist_0_rgb": True,
                "right_wrist_0_rgb": True,
            },
            "state": np.pad(state, (0, 18)),
        }
        tokens, mask = self._tokens(
            request["observation"].get("task_description", ""), data["state"]
        )
        data["tokenized_prompt"] = tokens
        data["tokenized_prompt_mask"] = mask
        return data

    def act_batch(self, requests):
        prepared = [self._prepare(request) for request in requests]
        batch = {
            key: self.torch.as_tensor(
                np.stack([item[key] for item in prepared]), device=self.device
            )
            for key in ("state", "tokenized_prompt", "tokenized_prompt_mask")
        }
        batch["image"] = {
            key: self.torch.as_tensor(
                np.stack([item["image"][key] for item in prepared]), device=self.device
            )
            for key in prepared[0]["image"]
        }
        batch["image_mask"] = {
            key: self.torch.as_tensor(
                np.stack([item["image_mask"][key] for item in prepared]), device=self.device
            )
            for key in prepared[0]["image_mask"]
        }
        observation = self.model_types.Observation.from_dict(batch)
        with self.torch.inference_mode():
            actions = self.model.sample_actions(
                self.device, observation, num_steps=self.inference_steps
            )
        actions = actions.float().cpu().numpy()
        outputs = []
        for request, state, chunk in zip(requests, prepared, actions, strict=True):
            action_stats = self.stats["actions"]
            mean = np.pad(action_stats["mean"], (0, 18))
            std = np.pad(action_stats["std"], (0, 18), constant_values=1.0)
            native = (chunk * (std + 1e-6) + mean)[:, :14]
            delta_mask = np.array(
                [1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 1, 0], bool
            )
            decoded_state = (
                state["state"][:14] * (self.stats["state"]["std"] + 1e-6)
                + self.stats["state"]["mean"]
            )
            native[:, delta_mask] += decoded_state[delta_mask]
            native *= np.array(
                [1, -1, -1, 1, 1, 1, 1, 1, -1, -1, 1, 1, 1, 1],
                np.float32,
            )
            native[:, [6, 13]] = (
                native[:, [6, 13]] + 0.5476 + 0.6213
            ) / (1.4910 + 0.6213)
            outputs.append(
                {
                    "request_id": request["request_id"],
                    "action": {
                        "native_actions": [
                            {"actions": action.tolist()}
                            for action in native[: self.action_chunk]
                        ]
                    },
                }
            )
        return outputs

    def close_session(self, session_id, result):
        self.sessions.discard(session_id)

    def close(self):
        self.sessions.clear()
