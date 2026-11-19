# Adapted from allenai/vla-evaluation-harness @ 6cc3e1bd9fc4bed26fb0a3aabd342c5fed11ca09.
# Apache-2.0; see LICENSE.upstream and NOTICE.upstream.
# Modified: RoboTenon import paths; standalone extension integration.
"""VLABench benchmark implementation.

VLABench is a large-scale benchmark for language-conditioned robotics
manipulation with long-horizon reasoning tasks, built on dm_control (MuJoCo).

Actions from the model server are 7-D: ``[dx, dy, dz, droll, dpitch, dyaw,
gripper]``.  These deltas are added to the current end-effector pose, then
converted to joint-space via inverse kinematics before being sent to the
dm_control environment as ``[7D qpos, 2D gripper]``.

Success is detected via dm_control's ``timestep.last()`` termination signal.
"""

from __future__ import annotations

import logging
import os
import json
from pathlib import Path
from typing import Any

import numpy as np

from robotenon_benchmark_support.base import StepBenchmark, StepResult
from robotenon_benchmark_support.render import configure_mujoco_render
from robotenon_benchmark_support.specs import GRIPPER_RAW, IMAGE_RGB, LANGUAGE, POSITION_DELTA, ROTATION_EULER, DimSpec
from robotenon_benchmark_support.types import Action, EpisodeResult, Observation, Task

logger = logging.getLogger(__name__)

os.environ.setdefault("MUJOCO_GL", "egl")

DEFAULT_TASKS = [
    "select_fruit",
    "select_toy",
    "select_drink",
    "select_book",
    "select_painting",
]

DEFAULT_MAX_STEPS = 200
DEFAULT_EVALUATION_TRACKS = [
    "track_1_in_distribution",
    "track_2_cross_category",
    "track_3_common_sense",
    "track_4_semantic_instruction",
    "track_6_unseen_texture",
]


class VLABenchBenchmark(StepBenchmark):
    """VLABench manipulation benchmark (dm_control / MuJoCo).

    Args:
        tasks: List of VLABench task names to evaluate.
        robot: Robot name (default ``"franka"``).
        max_steps: Maximum steps per episode (default 200).
    """

    _ALL_RECORD_FIELDS = frozenset({"reward", "done", "success"})

    render_backends = frozenset({"gpu", "cpu"})

    @classmethod
    def configure_render(cls, mode: str) -> dict[str, str]:
        return configure_mujoco_render(mode)

    def __init__(
        self,
        tasks: list[str] | None = None,
        robot: str = "franka",
        max_steps: int | None = None,
        max_steps_scale: float = 1.0,
        evaluation_tracks: list[str] | None = None,
        episode_config_dir: str | None = None,
    ) -> None:
        super().__init__()
        self._evaluation_tracks = list(evaluation_tracks or [])
        self._task_names = (
            list(tasks)
            if tasks is not None
            else ([] if self._evaluation_tracks else list(DEFAULT_TASKS))
        )
        self._robot = robot
        if max_steps is not None and (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or max_steps < 1
        ):
            raise ValueError("max_steps must be a positive integer or None")
        self._max_steps = max_steps
        if (
            isinstance(max_steps_scale, bool)
            or not isinstance(max_steps_scale, (int, float))
            or not np.isfinite(max_steps_scale)
            or max_steps_scale <= 0
        ):
            raise ValueError("max_steps_scale must be a positive finite number")
        self._max_steps_scale = float(max_steps_scale)
        self._episode_config_dir = episode_config_dir
        self._track_configs: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._official_task_configs: dict[str, Any] | None = None
        self._env: Any = None
        self._current_task: str | None = None
        self._instruction: str = ""
        self._last_ee_state: np.ndarray | None = None

    def cleanup(self) -> None:
        if self._env is not None:
            try:
                self._env.close()
            except Exception:
                pass
            self._env = None

    def _ensure_vlabench(self) -> None:
        """Lazy-import VLABench and register robots/tasks."""
        import VLABench  # noqa: F401 — triggers VLABENCH_ROOT
        import VLABench.robots  # noqa: F401 — registers robot classes
        import VLABench.tasks  # noqa: F401 — registers task classes

        # Monkey-patch to skip PCD generator (Open3D segfaults in headless
        # containers and we never request point clouds).
        from VLABench.envs.dm_env import LM4ManipDMEnv

        if not hasattr(LM4ManipDMEnv, "_pcd_patched"):
            # Stub that satisfies `self.pcd_generator.physics = ...`
            class _PcdStub:
                physics = None

            LM4ManipDMEnv.register_pcd_generator = lambda self: setattr(self, "pcd_generator", _PcdStub())
            LM4ManipDMEnv._pcd_patched = True

    def get_tasks(self) -> list[Task]:
        if self._evaluation_tracks:
            tasks: list[Task] = []
            for track in self._evaluation_tracks:
                config_path = self._track_config_path(track)
                config = self._load_track_config(track, config_path)
                for task_name in config:
                    if self._task_names and task_name not in self._task_names:
                        continue
                    tasks.append(
                        {
                            "name": f"{track}/{task_name}",
                            "task_name": task_name,
                            "suite": track,
                            "evaluation_track": track,
                            "episode_config_path": str(config_path),
                            "episode_count": len(config[task_name]),
                            "episode_max_steps": self._task_max_steps(task_name),
                        }
                    )
            return tasks
        return [
            {"name": task_name, "episode_max_steps": self._task_max_steps(task_name)}
            for task_name in self._task_names
        ]

    def _task_max_steps(self, task_name: str) -> int:
        """Return VLABench's official per-task evaluation horizon."""
        if self._max_steps is not None:
            return self._max_steps
        from VLABench.configs import name2config

        if self._official_task_configs is None:
            vlabench_root = os.environ.get("VLABENCH_ROOT")
            if not vlabench_root:
                raise RuntimeError(
                    "VLABENCH_ROOT is required for official VLABench task horizons"
                )
            path = Path(vlabench_root) / "configs" / "task_config.json"
            self._official_task_configs = json.loads(path.read_text())

        config_key = next(
            (
                key
                for key, names in name2config.items()
                if isinstance(names, list) and task_name in names
            ),
            task_name,
        )
        task_config = self._official_task_configs.get(config_key, {})
        return int(
            round(
                float(
                    task_config.get("evaluation", {}).get(
                        "max_episode_length", DEFAULT_MAX_STEPS
                    )
                )
                * self._max_steps_scale
            )
        )

    def _track_config_path(self, track: str) -> Path:
        root = self._episode_config_dir
        if root is None:
            vlabench_root = os.environ.get("VLABENCH_ROOT")
            if not vlabench_root:
                raise RuntimeError("VLABENCH_ROOT is required for official VLABench evaluation tracks")
            root = os.path.join(vlabench_root, "configs", "evaluation", "tracks")
        path = Path(root) / f"{track}.json"
        if not path.is_file():
            raise FileNotFoundError(f"VLABench evaluation track not found: {path}")
        return path

    def _load_track_config(self, track: str, path: Path) -> dict[str, list[dict[str, Any]]]:
        config = self._track_configs.get(track)
        if config is None:
            config = json.loads(path.read_text())
            self._track_configs[track] = config
        return config

    def reset(self, task: Task) -> Any:
        self._ensure_vlabench()
        from VLABench.envs import load_env

        task_name = task.get("task_name", task["name"])

        # Close previous env and create new one (task may change scene layout)
        if self._env is not None:
            try:
                self._env.close()
            except Exception as e:
                logger.warning("Failed to close VLABench environment: %s", e)
        load_kwargs: dict[str, Any] = {"robot": self._robot}
        track = task.get("evaluation_track")
        if track:
            path = Path(task.get("episode_config_path") or self._track_config_path(track))
            config = self._load_track_config(track, path)
            episode_idx = int(task.get("episode_idx", 0))
            episodes = config[task_name]
            if not 0 <= episode_idx < len(episodes):
                raise IndexError(
                    f"episode {episode_idx} outside {track}/{task_name} "
                    f"(available: {len(episodes)})"
                )
            load_kwargs.update(
                episode_config=episodes[episode_idx],
                random_init=False,
                # Xiaomi's official evaluator leaves eval_unseen at False for
                # every track. Track 6 variation is fixed by episode_config.
                eval=False,
                run_mode="eval",
            )
        self._env = load_env(task_name, **load_kwargs)
        self._current_task = task_name

        obs = self._env.get_observation(require_pcd=False)
        self._instruction = self._env.task.get_instruction()
        self._last_ee_state = obs.get("ee_state", None)
        self._recorder.record_video(self._extract_frame(obs))
        # Scene construction creates large temporary mesh/XML buffers.  At
        # high process counts CPython and glibc otherwise retain those freed
        # pages for the lifetime of every persistent simulator Worker.  Return
        # unreachable objects and free heap pages after the live environment
        # and initial observation have been retained.
        import gc

        gc.collect()
        try:
            import ctypes

            malloc_trim = ctypes.CDLL(None).malloc_trim
            malloc_trim.argtypes = [ctypes.c_size_t]
            malloc_trim.restype = ctypes.c_int
            malloc_trim(0)
        except (AttributeError, OSError):
            # Non-glibc platforms do not expose malloc_trim.
            pass
        return obs

    def step(self, action: Action) -> StepResult:
        from VLABench.utils.utils import euler_to_quaternion, quaternion_to_euler

        raw_action = action.get("actions", action.get("action"))
        if raw_action is None:
            raw_action = np.zeros(7, dtype=np.float32)
        raw_action = np.asarray(raw_action, dtype=np.float64)
        assert raw_action.shape[-1] == 7, f"Action dimension mismatch: got {raw_action.shape[-1]}, expected 7"

        gripper_cmd = raw_action[6] if len(raw_action) > 6 else 0.0
        control_mode = action.get("control_mode", "ee_delta")
        if control_mode == "ee_absolute":
            # Xiaomi freezes cumulative targets when each five-action plan is
            # produced. Do not rebase later actions on achieved simulator pose.
            target_pos = raw_action[:3]
            target_euler = raw_action[3:6]
        elif control_mode == "ee_delta":
            # Backward-compatible path for other VLABench policy backends.
            ee_state = self._last_ee_state
            if ee_state is None:
                ee_state = np.concatenate(
                    [self._env.get_ee_pos(), self._env.get_ee_quat(), [0.0]]
                )
            current_pos = ee_state[:3]
            current_quat = ee_state[3:7]
            current_euler = np.array(quaternion_to_euler(current_quat))
            target_pos = current_pos + raw_action[:3]
            target_euler = current_euler + raw_action[3:6]
        else:
            raise ValueError(f"unsupported VLABench control_mode: {control_mode!r}")
        target_quat = euler_to_quaternion(*target_euler)

        # Inverse kinematics: ee pose → joint positions
        _, qpos = self._env.robot.get_qpos_from_ee_pos(
            physics=self._env.physics,
            pos=target_pos,
            quat=target_quat,
        )

        # Gripper: >0 → open (0.04), ≤0 → closed (0)
        grip_val = 0.04 if gripper_cmd > 0 else 0.0
        gripper_state = np.array([grip_val, grip_val])
        env_action = np.concatenate([qpos, gripper_state])

        timestep = self._env.step(env_action)
        success = bool(timestep.last())

        # Update cached EE state
        obs = self._env.get_observation(require_pcd=False)
        self._last_ee_state = obs.get("ee_state", None)
        self._instruction = self._env.task.get_instruction()

        self._recorder.record_video(self._extract_frame(obs))
        self._recorder.record_step(reward=1.0 if success else 0.0, done=success, success=success)

        return StepResult(
            obs=obs,
            reward=1.0 if success else 0.0,
            done=success,
            info={"success": success, "timestep": timestep},
        )

    @staticmethod
    def _extract_frame(raw_obs: Any) -> np.ndarray | None:
        if not isinstance(raw_obs, dict):
            return None
        rgb = raw_obs.get("rgb")
        if rgb is None or len(rgb) == 0:
            return None
        return np.asarray(rgb[0])

    def make_obs(self, raw_obs: Any, task: Task) -> Observation:
        rgb = raw_obs.get("rgb", None)
        if rgb is None or len(rgb) < 4:
            raise ValueError("VLABench observation must contain at least four RGB cameras")
        if "ee_state" not in raw_obs:
            raise KeyError("VLABench observation is missing ee_state")

        # Xiaomi's official adapter uses camera indices 2, 0 and 3 in this
        # semantic order.  Preserve names on the wire so model backends do not
        # depend on VLABench's positional camera list.
        second_image, _, front_image, wrist_image = rgb
        images = {
            "primary": np.ascontiguousarray(second_image),
            "ego": np.ascontiguousarray(front_image),
            "base": np.ascontiguousarray(second_image),
            "left_wrist": np.ascontiguousarray(wrist_image),
        }

        return {
            "images": images,
            "state": {"ee_state": np.asarray(raw_obs["ee_state"], dtype=np.float32)},
            "task_description": self._instruction,
        }

    def check_done(self, step_result: StepResult) -> bool:
        return step_result.done

    def get_step_result(self, step_result: StepResult) -> EpisodeResult:
        result: EpisodeResult = {"success": step_result.info.get("success", False)}
        if self._env is not None:
            result["intention_score"] = float(self._env.get_intention_score(threshold=0.1))
            result["progress_score"] = float(self._env.get_task_progress())
        return result

    def get_metric_keys(self) -> dict[str, str]:
        return {
            "success": "mean",
            "intention_score": "mean",
            "progress_score": "mean",
        }

    def get_metadata(self) -> dict[str, Any]:
        task_horizons = [task["episode_max_steps"] for task in self.get_tasks()]
        metadata: dict[str, Any] = {
            "max_steps": max(task_horizons, default=DEFAULT_MAX_STEPS)
        }
        if self._evaluation_tracks:
            counts = [
                len(episodes)
                for track in self._evaluation_tracks
                for episodes in self._load_track_config(track, self._track_config_path(track)).values()
            ]
            if counts:
                metadata["max_episodes_per_task"] = min(counts)
        return metadata

    def get_action_spec(self) -> dict[str, DimSpec]:
        return {
            "position": POSITION_DELTA,
            "rotation": ROTATION_EULER,
            "gripper": GRIPPER_RAW,
        }

    def get_observation_spec(self) -> dict[str, DimSpec]:
        return {
            "primary": IMAGE_RGB,
            "image": IMAGE_RGB,
            "state": DimSpec("state", 8, "eef_pos3_quat_wxyz4_gripper1"),
            "language": LANGUAGE,
        }
