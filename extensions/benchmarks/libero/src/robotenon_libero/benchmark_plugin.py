"""RoboTenon providers for the standalone libero benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import LIBEROBenchmark

Environment, Body, Tasks = register_benchmark(
    "libero_benchmark", LIBEROBenchmark, module=__name__,
    dimensions={"suite": "task.suite", "task": "task.name"},
)
