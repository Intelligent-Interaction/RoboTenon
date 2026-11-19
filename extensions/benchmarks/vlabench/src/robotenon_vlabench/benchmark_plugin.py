"""RoboTenon providers for the standalone vlabench benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import VLABenchBenchmark

Environment, Body, Tasks = register_benchmark(
    "vlabench_benchmark", VLABenchBenchmark, module=__name__,
    dimensions={"task": "task.name", "robot": "params.robot"},
)
