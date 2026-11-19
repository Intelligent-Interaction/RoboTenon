"""RoboTenon providers for the standalone robocerebra benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import RoboCerebraBenchmark

Environment, Body, Tasks = register_benchmark(
    "robocerebra_benchmark", RoboCerebraBenchmark, module=__name__,
    dimensions={"task_type": "task.task_type", "case": "task.case_name"},
)
