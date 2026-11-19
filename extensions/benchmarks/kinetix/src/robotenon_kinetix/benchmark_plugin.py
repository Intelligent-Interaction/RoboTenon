"""RoboTenon providers for the standalone kinetix benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import KinetixBenchmark

Environment, Body, Tasks = register_benchmark(
    "kinetix_benchmark", KinetixBenchmark, module=__name__,
    dimensions={"level": "task.level"},
)
