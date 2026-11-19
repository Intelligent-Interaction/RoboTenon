"""RoboTenon providers for the standalone molmospaces benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import MolmoSpacesBenchmark

Environment, Body, Tasks = register_benchmark(
    "molmospaces_benchmark", MolmoSpacesBenchmark, module=__name__,
    dimensions={"task_type": "task.task_cls", "task": "task.name"},
)
