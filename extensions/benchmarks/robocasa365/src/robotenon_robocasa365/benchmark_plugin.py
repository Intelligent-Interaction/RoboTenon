"""RoboTenon providers for the standalone robocasa365 benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import RoboCasa365Benchmark

Environment, Body, Tasks = register_benchmark(
    "robocasa365_benchmark", RoboCasa365Benchmark, module=__name__,
    dimensions={"suite": "task.suite", "task": "task.name"},
)
