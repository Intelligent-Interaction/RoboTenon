"""Single-request OpenVLA-OFT adapter for original LIBERO-Spatial."""

SPEC = {
    "api_version": 1,
    "supported_benchmarks": ["libero"],
    "supports_batch": False,
    "history": {"length": 1, "interval": 1},
    "replan_steps": 8,
}

# The adapter returns actions already converted to LIBERO's native convention.
# Explicit profile values take precedence over these defaults in RoboTenon.
OBSERVATION_PARAMS = {
    "send_wrist_image": True,
    "send_state": True,
    "quat_no_antipodal": True,
    "discretize_gripper": False,
    "env_seed": 0,
}


def create(**params):
    # Metadata checks must not import Torch/TF, initialize CUDA or load weights.
    from .implementation import OpenVLAOFT

    return OpenVLAOFT(**params)
