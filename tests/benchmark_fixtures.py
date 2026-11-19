"""Deterministic environment fixture for protocol tests (not a simulator)."""
import numpy as np
from robotenon.benchmarks.base import StepBenchmark, StepResult
from robotenon.benchmarks.specs import DimSpec, LANGUAGE
from robotenon.benchmarks.integration import register_benchmark

class ContractBenchmark(StepBenchmark):
    def __init__(self, done_after=3, seed=7):
        super().__init__()
        self.done_after, self.seed = done_after, seed
        self.step_count = 0
        self.obs_calls = 0
        self.closed = False
    def get_tasks(self):
        return [{'name':'first', 'suite':'test'}, {'name':'second','suite':'test'}]
    def reset(self, task):
        self.step_count = self.obs_calls = 0
        self.task = task
        return np.zeros((2,3,3), dtype=np.uint8)
    def step(self, action):
        assert action == {'actions': [1.0]}
        self.step_count += 1
        done = self.step_count >= self.done_after
        self._recorder.record_step(reward=1.0, done=done, success=done)
        return StepResult(np.full((2,3,3),self.step_count,dtype=np.uint8),1.0,done,{})
    def make_obs(self, raw_obs, task):
        self.obs_calls += 1
        return {'images':{'front':raw_obs}, 'task_description':task['name'],
                'states':np.array([self.step_count], dtype=np.float32),
                'first_only':self.obs_calls==1,
                'video_history':[raw_obs.copy()] if self.obs_calls==1 else []}
    def get_step_result(self, result):
        return {'success': result.done, 'completed_subtasks': self.step_count}
    def get_metric_keys(self):
        return {'success':'mean','completed_subtasks':'mean'}
    def get_metadata(self):
        return {'max_steps':5,'max_episodes_per_task':2}
    def get_action_spec(self):
        return {'joints':DimSpec('joints',1,'test')}
    def get_observation_spec(self):
        return {'language':LANGUAGE}
    def cleanup(self):
        self.closed = True

Environment, Body, Tasks = register_benchmark('contract_benchmark',ContractBenchmark,module=__name__)

class ConstantPolicy:
    def reset(self, task):
        self.calls = 0
    def predict(self, observation):
        assert observation['first_only'] == (self.calls == 0)
        assert len(observation['video_history']) == (1 if self.calls == 0 else 0)
        assert observation['images']['front'].dtype == np.uint8
        self.calls += 1
        return {'actions':[1.0]}

def create_policy():
    return ConstantPolicy()


class NoisyBenchmark(ContractBenchmark):
    def get_tasks(self):
        import os
        os.write(1, b'native simulator diagnostic on stdout\n')
        return super().get_tasks()


VLA_EVENTS = []

class RecordingVLA:
    def start_episode(self, task_info):
        VLA_EVENTS.append(('start', task_info))
        self.policy = ConstantPolicy()
        self.policy.reset(task_info)
    def act(self, observation):
        VLA_EVENTS.append(('act', observation['first_only']))
        return self.policy.predict(observation)
    def end_episode(self, result):
        VLA_EVENTS.append(('end', result))
    def close(self):
        VLA_EVENTS.append(('close', None))


def create_recording_vla():
    return RecordingVLA()

class FailingVLA(RecordingVLA):
    def act(self, observation):
        raise RuntimeError('test VLA inference failure')

def create_failing_vla():
    return FailingVLA()
