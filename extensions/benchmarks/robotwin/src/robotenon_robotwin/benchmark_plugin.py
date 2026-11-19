"""RoboTenon providers for the standalone robotwin benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import RoboTwinBenchmark

Environment, Body, Tasks = register_benchmark(
    "robotwin_benchmark", RoboTwinBenchmark, module=__name__,
    dimensions={"suite": "task.suite", "task": "task.name"},
)
