"""Stateful deterministic protocol fixture, never a simulator substitute."""
from copy import deepcopy
import numpy as np
from robotenon_benchmark_support.base import StepBenchmark, StepResult
from robotenon_benchmark_support.specs import DimSpec, LANGUAGE
from robotenon_benchmark_support.integration import register_benchmark

EVENTS = []
INSTANCES = []

class StatefulBenchmark(StepBenchmark):
    def __init__(self, done_after=3):
        super().__init__()
        self.rng = np.random.default_rng(123)
        self.done_after = done_after
        self.steps = 0
        self.cleaned = 0
        self.task_reads = 0
        INSTANCES.append(self)
    def get_tasks(self):
        self.task_reads += 1
        return [{'name':'first'}, {'name':'second'}]
    def get_metadata(self):
        return {'max_steps': 3, 'max_episodes_per_task': 2}
    def get_action_spec(self):
        return {'actions':DimSpec('actions', 1, 'test')}
    def get_observation_spec(self):
        return {'language': LANGUAGE}
    def reset(self, task):
        self.steps = 0
        self.task = deepcopy(task)
        self.value = int(self.rng.integers(0, 100))
        EVENTS.append(('reset', deepcopy(task), self.value))
        return self.frame()
    def frame(self):
        return np.full((2,3,3), self.value, dtype=np.uint8)
    def make_obs(self, raw, task):
        EVENTS.append(('observe', self.steps, self.value))
        return {'images':{'front':raw}, 'state':np.array([self.value],dtype=np.float32),
                'task_description':task['name']}
    def step(self, action):
        EVENTS.append(('action', deepcopy(action)))
        self.value += int(action['actions'][0])
        self.steps += 1
        return StepResult(self.frame(), 1., self.steps >= self.done_after, {})
    async def is_done(self):
        done = await super().is_done()
        EVENTS.append(('done', done))
        return done
    async def get_time(self):
        EVENTS.append(('time', self.steps))
        return self.steps * .1
    def get_step_result(self, result):
        EVENTS.append(('metrics', self.steps))
        return {'success':result.done, 'state':self.value}
    def get_metric_keys(self):
        return {'success':'mean', 'state':'mean'}
    def cleanup(self):
        self.cleaned += 1

Environment, Body, Tasks = register_benchmark(
    'parity_benchmark', StatefulBenchmark, module=__name__,
    dimensions={'task': 'task.name'},
)

class VLA:
    def start_episode(self, info):
        EVENTS.append(('vla_start', deepcopy(info['task'])))
    def act(self, obs):
        assert obs['images']['front'].dtype == np.uint8
        assert obs['state'].dtype == np.float32
        EVENTS.append(('act', int(obs['images']['front'][0,0,0]), float(obs['state'][0])))
        return {'actions':[1.]}
    def end_episode(self, result):
        # RoboTenon adds status; upstream metrics/steps/time must be identical.
        result = {k:v for k,v in result.items() if k != 'status'}
        EVENTS.append(('vla_end', deepcopy(result)))
    def close(self):
        pass

def create():
    return VLA()
