"""Replaceable synchronous VLA interface and deterministic native-test policy.

Factories return a VLA with start_episode(task_info), act(observation), and
end_episode(result). Methods may be synchronous or asynchronous. Observations
and actions follow the selected benchmark's native protocol, without reshaping.
"""
from copy import deepcopy
from typing import Any, Protocol
from .modules import validate_native_action


class NativeVLA(Protocol):
    def start_episode(self, task_info: dict[str, Any]) -> Any: ...
    def act(self, observation: dict[str, Any]) -> Any: ...
    def end_episode(self, result: dict[str, Any]) -> Any: ...


class FakeVLA:
    """A fake model; the environment, action execution and metrics remain real."""
    def __init__(self, *, action):
        validate_native_action(action)
        self.action = deepcopy(action)
        self.active = False
        self.calls = 0

    def start_episode(self, task_info):
        if self.active:
            raise RuntimeError('previous fake VLA episode is still active')
        self.active = True
        self.calls = 0
        self.task_info = deepcopy(task_info)

    def act(self, observation):
        if not self.active:
            raise RuntimeError('start_episode must precede act')
        if not isinstance(observation, dict) or not observation:
            raise ValueError('native observation must be a nonempty mapping')
        self.calls += 1
        return deepcopy(self.action)

    def end_episode(self, result):
        if not self.active:
            raise RuntimeError('no active fake VLA episode')
        self.last_result = deepcopy(result)
        self.active = False

    def close(self):
        self.active = False


def create(*, action):
    return FakeVLA(action=action)


class FakeBatchVLA:
    """Batch-shaped fake policy; implement act_batch for real model inference."""
    def __init__(self, *, action):
        validate_native_action(action)
        self.action = deepcopy(action)
        self.sessions = {}

    def open_session(self, session_id, task_info):
        if session_id in self.sessions:
            raise RuntimeError('duplicate fake VLA session')
        self.sessions[session_id] = deepcopy(task_info)

    def act_batch(self, requests):
        responses = []
        for request in requests:
            if request['session_id'] not in self.sessions:
                raise RuntimeError('unknown fake VLA session')
            if not isinstance(request['observation'], dict) or not request['observation']:
                raise ValueError('native observation must be a nonempty mapping')
            responses.append({'request_id': request['request_id'], 'action': deepcopy(self.action)})
        return responses

    def close_session(self, session_id, result):
        self.sessions.pop(session_id, None)

    def close(self):
        self.sessions.clear()
