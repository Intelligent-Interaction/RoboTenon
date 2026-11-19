"""RoboTenon providers for the standalone duobench benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import DuoBenchBenchmark

Environment, Body, Tasks = register_benchmark(
    "duobench_benchmark", DuoBenchBenchmark, module=__name__,
    dimensions={"task": "task.task_id"},
)
