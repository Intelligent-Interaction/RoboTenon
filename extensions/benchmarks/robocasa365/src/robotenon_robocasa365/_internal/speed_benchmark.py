"""Controlled throughput scene; not an official full-suite evaluation profile."""

import hashlib
import json
from pathlib import Path

import numpy as np
from robotenon_benchmark_support.integration import register_benchmark
from robotenon_robocasa365.benchmark import RoboCasa365Benchmark


class ControlledRoboCasa365(RoboCasa365Benchmark):
    def __init__(self, *, audit_dir=None, **kwargs):
        super().__init__(**kwargs)
        self.audit_dir = audit_dir

    def _make_env(self, task_name):
        import gymnasium as gym
        import robocasa.wrappers.gym_wrapper  # noqa: F401

        return gym.make(
            f"robocasa/{task_name}",
            split=self._split,
            seed=self._seed,
            layout_ids=[42],
            style_ids=[53],
            enable_render=True,
            camera_widths=self._camera_size,
            camera_heights=self._camera_size,
            disable_env_checker=True,
        )

    def reset(self, task):
        observation = super().reset(task)
        if self.audit_dir:
            directory = Path(self.audit_dir)
            directory.mkdir(parents=True, exist_ok=True)
            states = {
                k: np.asarray(v).tolist()
                for k, v in observation.items()
                if k.startswith("state.")
            }
            images = {
                k: hashlib.sha256(np.asarray(v).tobytes()).hexdigest()
                for k, v in observation.items()
                if k.startswith("video.")
            }
            record = {"task": task, "states": states, "images": images}
            (
                directory / f"{task['name']}-{task.get('episode_idx', 0)}.json"
            ).write_text(json.dumps(record, indent=2))
        return observation


Environment, Body, Tasks = register_benchmark(
    "xiaomi_speed_benchmark", ControlledRoboCasa365, module=__name__
)
