"""RoboTenon providers for the standalone libero_plus benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import LIBEROPlusBenchmark

Environment, Body, Tasks = register_benchmark(
    "libero_plus_benchmark", LIBEROPlusBenchmark, module=__name__,
    dimensions={"suite": "task.suite", "category": "task.category",
                "difficulty": "task.difficulty_level", "task": "task.name"},
)
