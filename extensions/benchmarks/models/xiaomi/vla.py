"""Xiaomi Robotics 1 backend; weights are loaded once by create()."""
from .implementation import XiaomiBatchVLA

SPEC = {
    "api_version": 1,
    "supported_benchmarks": ["robocasa365", "vlabench"],
    "supports_batch": True,
    "history": {"length": 4, "interval": 2},
    "replan_steps": 16,
    "benchmark_overrides": {
        "vlabench": {
            "history": {"length": 1, "interval": 1},
            "replan_steps": 5,
        }
    },
}


def create(**params):
    # Temporal contracts are selected from SPEC by the coordinator.  Keep
    # backend configuration about model/preprocessing, not runtime scheduling.
    for key in ("obs_history", "obs_interval", "replan_steps"):
        if key in params:
            raise ValueError(f"{key} is benchmark-owned; remove it from backend config")
    return XiaomiBatchVLA(**params)
