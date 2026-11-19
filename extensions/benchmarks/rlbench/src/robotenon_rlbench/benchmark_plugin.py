"""RoboTenon providers for the standalone rlbench benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import RLBenchBenchmark

Environment, Body, Tasks = register_benchmark(
    "rlbench_benchmark", RLBenchBenchmark, module=__name__,
    dimensions={"task": "task.task_file"},
)
