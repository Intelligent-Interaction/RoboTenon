"""OpenPI pi0.5 backend for the RoboTwin 2.0 benchmark."""

from .implementation import PI05RoboTwin2VLA


SPEC = {
    "api_version": 1,
    "supported_benchmarks": ["robotwin"],
    "supports_batch": True,
    "history": {"length": 1, "interval": 1},
    "replan_steps": 8,
}

OBSERVATION_PARAMS = {"send_state": True, "send_wrist_image": True}


def create(**params):
    return PI05RoboTwin2VLA(**params)
