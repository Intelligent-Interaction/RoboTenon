"""RoboTenon providers for the standalone robocasa benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import RoboCasaBenchmark

Environment, Body, Tasks = register_benchmark(
    "robocasa_benchmark", RoboCasaBenchmark, module=__name__,
    dimensions={"task": "task.name"},
)
