"""RoboTenon providers for the standalone calvin benchmark."""
from robotenon_benchmark_support.integration import register_benchmark
from .benchmark import CALVINBenchmark

Environment, Body, Tasks = register_benchmark(
    "calvin_benchmark", CALVINBenchmark, module=__name__,
    dimensions={"sequence": "task.eval_sequence", "initial_condition": "task.initial_condition"},
)
