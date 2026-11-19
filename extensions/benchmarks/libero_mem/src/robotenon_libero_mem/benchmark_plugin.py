"""RoboTenon providers for the standalone libero_mem benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import LIBEROMemBenchmark

Environment, Body, Tasks = register_benchmark(
    "libero_mem_benchmark", LIBEROMemBenchmark, module=__name__,
    dimensions={"suite": "task.suite", "task": "task.name"},
)
