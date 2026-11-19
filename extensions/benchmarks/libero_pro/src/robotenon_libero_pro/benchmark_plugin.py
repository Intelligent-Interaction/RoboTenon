"""RoboTenon providers for the standalone libero_pro benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import LIBEROProBenchmark

Environment, Body, Tasks = register_benchmark(
    "libero_pro_benchmark", LIBEROProBenchmark, module=__name__,
    dimensions={"suite": "task.suite", "task": "task.name"},
)
