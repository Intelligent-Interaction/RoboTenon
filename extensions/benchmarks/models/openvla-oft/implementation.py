"""RoboTenon session protocol around the pinned official OFT inference helpers."""

from __future__ import annotations

import json
from importlib.metadata import distribution
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np


UPSTREAM_COMMIT = "e4287e94541f459edc4feabc4e181f537cd569a8"
TRANSFORMERS_COMMIT = "bc339d9ad707454c0c115970db43c260067c61ab"
SUITE = "libero_spatial"


def prepare_observation(observation):
    """Map upright benchmark frames; official helpers own resize/crop/normalize."""
    history = observation.get("history")
    if not isinstance(history, (list, tuple)) or len(history) != 1:
        raise ValueError("OpenVLA-OFT requires exactly one observation frame")
    frame = history[0]
    result = {}
    for source, destination in (("agentview", "full_image"), ("wrist", "wrist_image")):
        image = np.asarray(frame.get("images", {}).get(source))
        if image.shape != (256, 256, 3) or image.dtype != np.uint8:
            raise ValueError(f"OpenVLA-OFT {source} must be uint8 RGB (256, 256, 3)")
        result[destination] = np.ascontiguousarray(image)
    state = np.asarray(frame.get("states"))
    if state.shape != (8,) or not np.issubdtype(state.dtype, np.number) or not np.isfinite(state).all():
        raise ValueError("OpenVLA-OFT requires a finite numeric 8-D proprio state")
    # Official get_vla_action replaces obs['state'] during normalization.
    result["state"] = state.copy()
    instruction = observation.get("task_description")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("OpenVLA-OFT requires a nonempty task_description")
    return result, instruction


def native_actions(raw_actions):
    """Official normalize_gripper_action(binarize=True), then invert its sign."""
    actions = np.asarray(raw_actions)
    if actions.shape != (8, 7) or not np.issubdtype(actions.dtype, np.number) or not np.isfinite(actions).all():
        raise ValueError("OpenVLA-OFT must return a finite numeric (8, 7) action chunk")
    actions = actions.copy()
    actions[:, -1] = -np.sign(2 * actions[:, -1] - 1)
    return [{"actions": action.tolist()} for action in actions]


class OpenVLAOFT:
    def __init__(self, model_path, upstream_path, *, seed=7):
        self.sessions = set()
        self._load(Path(model_path).expanduser().resolve(), Path(upstream_path).expanduser().resolve(), seed)

    def _load(self, model_path, upstream_path, seed):
        if not model_path.is_dir():
            raise FileNotFoundError(f"OpenVLA-OFT checkpoint not found: {model_path}")
        commit = subprocess.check_output(
            ["git", "-C", str(upstream_path), "rev-parse", "HEAD"], text=True
        ).strip()
        if commit != UPSTREAM_COMMIT:
            raise ValueError(f"OpenVLA-OFT source must be at {UPSTREAM_COMMIT}, got {commit}")
        subprocess.run(
            ["git", "-C", str(upstream_path), "diff", "--exit-code", "HEAD", "--", "prismatic", "experiments/robot"],
            check=True, stdout=subprocess.DEVNULL,
        )
        origin = json.loads(distribution("transformers").read_text("direct_url.json") or "{}")
        if origin.get("vcs_info", {}).get("commit_id") != TRANSFORMERS_COMMIT:
            raise ValueError("Install the pinned transformers-openvla-oft Git dependency from requirements.txt")
        sys.path.insert(0, str(upstream_path))

        import tensorflow as tf

        # TF is used only for the official JPEG/resize/crop preprocessing.
        tf.config.set_visible_devices([], "GPU")
        import torch
        from transformers import AutoConfig, AutoImageProcessor, AutoProcessor
        from experiments.robot import openvla_utils as utils
        from experiments.robot.robot_utils import set_seed_everywhere
        from prismatic.extern.hf.configuration_prismatic import OpenVLAConfig
        from prismatic.extern.hf.modeling_prismatic import OpenVLAForActionPrediction
        from prismatic.extern.hf.processing_prismatic import PrismaticImageProcessor, PrismaticProcessor
        from prismatic.models.action_heads import L1RegressionActionHead
        from prismatic.models.projectors import ProprioProjector
        from prismatic.vla.constants import ACTION_DIM, NUM_ACTIONS_CHUNK, PROPRIO_DIM, ACTION_PROPRIO_NORMALIZATION_TYPE

        for module in (utils, sys.modules[OpenVLAForActionPrediction.__module__]):
            if not Path(module.__file__).resolve().is_relative_to(upstream_path):
                raise RuntimeError("A different OpenVLA-OFT source package is already imported")
        if (ACTION_DIM, NUM_ACTIONS_CHUNK, PROPRIO_DIM, ACTION_PROPRIO_NORMALIZATION_TYPE.value) != (7, 8, 8, "bounds_q99"):
            raise ValueError("Official OpenVLA-OFT constants do not match LIBERO")
        if not torch.cuda.is_available():
            raise RuntimeError("OpenVLA-OFT weight loading requires an explicitly authorized CUDA run")

        statistics = json.loads((model_path / "dataset_statistics.json").read_text())
        unnorm_key = SUITE if SUITE in statistics else SUITE + "_no_noops"
        if unnorm_key not in statistics or "proprio" not in statistics[unnorm_key]:
            raise ValueError(f"Checkpoint lacks {SUITE} action/proprio statistics")
        # Resolve component files before allocating the large model.
        head_path = utils.find_checkpoint_file(str(model_path), "action_head")
        proprio_path = utils.find_checkpoint_file(str(model_path), "proprio_projector")
        set_seed_everywhere(seed)
        self.cfg = SimpleNamespace(
            pretrained_checkpoint=str(model_path), use_l1_regression=True, use_diffusion=False,
            use_film=False, num_images_in_input=2, use_proprio=True, center_crop=True,
            load_in_8bit=False, load_in_4bit=False, unnorm_key=unnorm_key,
        )
        # Use official local classes directly. get_vla() would rewrite checkpoint
        # config/source files; these registrations and loads leave them intact.
        AutoConfig.register("openvla", OpenVLAConfig, exist_ok=True)
        AutoImageProcessor.register(OpenVLAConfig, PrismaticImageProcessor, exist_ok=True)
        AutoProcessor.register(OpenVLAConfig, PrismaticProcessor, exist_ok=True)
        config = OpenVLAConfig.from_pretrained(str(model_path), local_files_only=True)
        self.model = OpenVLAForActionPrediction.from_pretrained(
            str(model_path), config=config, torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True, local_files_only=True,
        ).to(utils.DEVICE).eval()
        self.model.vision_backbone.set_num_images_in_input(2)
        self.model.norm_stats = statistics
        self.processor = AutoProcessor.from_pretrained(
            str(model_path), trust_remote_code=False, local_files_only=True
        )
        self.action_head = L1RegressionActionHead(
            input_dim=self.model.llm_dim, hidden_dim=self.model.llm_dim, action_dim=7
        ).to(dtype=torch.bfloat16, device=utils.DEVICE).eval()
        self.action_head.load_state_dict(utils.load_component_state_dict(head_path))
        self.proprio_projector = ProprioProjector(llm_dim=self.model.llm_dim, proprio_dim=8).to(
            dtype=torch.bfloat16, device=utils.DEVICE
        ).eval()
        self.proprio_projector.load_state_dict(utils.load_component_state_dict(proprio_path))
        self.utils = utils

    def open_session(self, session_id, task_info):
        if session_id in self.sessions:
            raise ValueError("OpenVLA-OFT session already exists")
        if task_info.get("benchmark_id") != "libero_benchmark" or task_info.get("task", {}).get("suite") != SUITE:
            raise ValueError("This OpenVLA-OFT adapter supports original LIBERO-Spatial only")
        self.sessions.add(session_id)

    def _predict(self, observation, instruction):
        return self.utils.get_vla_action(
            self.cfg, self.model, self.processor, observation, instruction,
            action_head=self.action_head, proprio_projector=self.proprio_projector,
        )

    def act_batch(self, requests):
        if not requests:
            return []
        if len(requests) != 1:
            raise ValueError("OpenVLA-OFT initial adapter requires batch_size=1")
        request = requests[0]
        if request["session_id"] not in self.sessions:
            raise ValueError("OpenVLA-OFT request references an unopened session")
        observation, instruction = prepare_observation(request["observation"])
        return [{"request_id": request["request_id"], "action": {
            "native_actions": native_actions(self._predict(observation, instruction))
        }}]

    def close_session(self, session_id, result):
        self.sessions.discard(session_id)

    def close(self):
        self.sessions.clear()
        for name in ("model", "processor", "action_head", "proprio_projector"):
            setattr(self, name, None)
