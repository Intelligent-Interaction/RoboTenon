"""Xiaomi Robotics 0 backend for the official LIBERO checkpoint."""

from .implementation import XiaomiRobotics0BatchVLA


SPEC = {
    "api_version": 1,
    "supported_benchmarks": ["libero", "libero_plus", "libero_pro"],
    "supports_batch": True,
    "history": {"length": 1, "interval": 1},
    "replan_steps": 10,
}

# Match Xiaomi's LIBERO evaluator: two 256px views, proprioception, and the
# checkpoint's continuous seven-dimensional action output.
OBSERVATION_PARAMS = {
    "send_wrist_image": True,
    "send_state": True,
    "discretize_gripper": False,
}


def create(**params):
    return XiaomiRobotics0BatchVLA(**params)
