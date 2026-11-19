"""LeRobot PI0.5 backend for the LIBERO benchmark."""

from .implementation import PI05BatchVLA


SPEC = {
    "api_version": 1,
    "supported_benchmarks": ["libero"],
    "supports_batch": True,
    "history": {"length": 1, "interval": 1},
    "replan_steps": 10,
}

# Match LeRobot's LIBERO environment processor and direct action execution.
OBSERVATION_PARAMS = {
    "send_wrist_image": True,
    "send_state": True,
    "discretize_gripper": False,
}


def create(**params):
    return PI05BatchVLA(**params)
