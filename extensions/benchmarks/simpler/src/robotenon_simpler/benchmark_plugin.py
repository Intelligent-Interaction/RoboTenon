"""RoboTenon providers for the standalone simpler benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import SimplerEnvBenchmark

Environment, Body, Tasks = register_benchmark(
    "simpler_benchmark", SimplerEnvBenchmark, module=__name__,
    dimensions={"task": "task.task_name", "environment": "params.env_name",
                "scene": "params.scene_name", "robot": "params.env_build_kwargs.robot"},
)
