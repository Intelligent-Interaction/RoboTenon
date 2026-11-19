"""RoboTenon providers for the standalone behavior1k benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import Behavior1KBenchmark

Environment, Body, Tasks = register_benchmark(
    "behavior1k_benchmark", Behavior1KBenchmark, module=__name__,
    dimensions={"suite": "task.suite", "task": "task.task_name"},
)
