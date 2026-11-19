"""RoboTenon providers for the standalone maniskill2 benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import ManiSkill2Benchmark

Environment, Body, Tasks = register_benchmark(
    "maniskill2_benchmark", ManiSkill2Benchmark, module=__name__,
    dimensions={"environment": "task.env_name"},
)
