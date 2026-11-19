"""RoboTenon providers for the standalone robodojo benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import RoboDojoBenchmark

Environment, Body, Tasks = register_benchmark(
    "robodojo_benchmark", RoboDojoBenchmark, module=__name__,
    dimensions={"suite": "task.suite", "task": "task.name"},
)
