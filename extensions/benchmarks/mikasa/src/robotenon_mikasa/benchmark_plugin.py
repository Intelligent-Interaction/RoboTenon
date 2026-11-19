"""RoboTenon providers for the standalone mikasa benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import MIKASABenchmark

Environment, Body, Tasks = register_benchmark(
    "mikasa_benchmark", MIKASABenchmark, module=__name__,
    dimensions={"task": "task.name"},
)
