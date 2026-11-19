"""Optional deterministic initialization, outside the upstream native adapter."""
import logging
import random
from copy import copy
from functools import partial
from types import FunctionType

import numpy as np

from ._internal.upstream_benchmark import VLABenchBenchmark as NativeBenchmark

logger = logging.getLogger(__name__)


def seeded_load_env(loader, seed, task, robot):
    """Supply Composer's private RNG without patching global library classes.

    VLABench's loader forwards **kwargs to the task, not the environment. Bind
    its environment constructor in a private function globals mapping instead.
    Preserve global RNGs so resetting a simulator cannot reseed the policy.
    """
    namespace = copy(loader.__globals__)
    namespace['LM4ManipDMEnv'] = partial(namespace['LM4ManipDMEnv'], random_state=seed)
    seeded_loader = FunctionType(loader.__code__, namespace, loader.__name__, loader.__defaults__, loader.__closure__)
    seeded_loader.__kwdefaults__ = loader.__kwdefaults__
    python_state, numpy_state = random.getstate(), np.random.get_state()
    try:
        random.seed(seed)
        np.random.seed(seed)
        return seeded_loader(task, robot=robot)
    finally:
        random.setstate(python_state)
        np.random.set_state(numpy_state)


class VLABenchBenchmark(NativeBenchmark):
    def __init__(self, tasks=None, robot='franka', max_steps=None, max_steps_scale=1.0, seed=None,
                 evaluation_tracks=None, episode_config_dir=None):
        super().__init__(tasks=tasks, robot=robot, max_steps=max_steps,
                         max_steps_scale=max_steps_scale, evaluation_tracks=evaluation_tracks,
                         episode_config_dir=episode_config_dir)
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32):
            raise ValueError('seed must be an integer in [0, 2**32), or None')
        self._seed = seed

    def reset(self, task):
        if self._seed is None or task.get('evaluation_track'):
            return super().reset(task)
        self._ensure_vlabench()
        from VLABench.envs import load_env

        if self._env is not None:
            try:
                self._env.close()
            except Exception as exc:
                logger.warning('Failed to close VLABench environment: %s', exc)
        seed = (self._seed + int(task.get('episode_idx', 0))) % (2**32)
        self._env = seeded_load_env(load_env, seed, task['name'], self._robot)
        self._current_task = task['name']
        obs = self._env.get_observation(require_pcd=False)
        self._instruction = self._env.task.get_instruction()
        self._last_ee_state = obs.get('ee_state', None)
        self._recorder.record_video(self._extract_frame(obs))
        return obs
