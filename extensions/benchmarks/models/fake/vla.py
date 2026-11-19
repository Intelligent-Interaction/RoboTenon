"""Protocol-only fake: configure a benchmark-compatible native action explicitly."""
from copy import deepcopy

SPEC = {"api_version": 1, "supported_benchmarks": ["*"], "supports_batch": True,
        "history": {"length": 1, "interval": 1}, "replan_steps": 1}


class VLA:
    def __init__(self, action):
        self.action = deepcopy(action)
        self.sessions = {}

    def open_session(self, session_id, task_info):
        if session_id in self.sessions:
            raise ValueError("duplicate session")
        self.sessions[session_id] = task_info

    def act_batch(self, requests):
        for request in requests:
            if request['session_id'] not in self.sessions:
                raise ValueError('unknown session')
        return [{"request_id": r["request_id"], "action": {
            "native_actions": [deepcopy(self.action)]}} for r in requests]

    def close_session(self, session_id, result):
        self.sessions.pop(session_id, None)

    def close(self):
        self.sessions.clear()


def create(**params):
    return VLA(**params)
