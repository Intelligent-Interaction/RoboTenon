"""RoboTenon providers for the standalone robomme benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import RoboMMEBenchmark

Environment, Body, Tasks = register_benchmark(
    "robomme_benchmark", RoboMMEBenchmark, module=__name__,
    dimensions={"environment": "task.env_id"},
)
