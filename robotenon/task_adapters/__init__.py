"""Registered Task Adapt providers and their execution contracts."""

from .base import (
    BaseTaskAdapter,
    ConfiguredTaskAdapter,
    TaskAdapter,
    TaskAdapterDescriptor,
    TaskAdapterRunContext,
    TaskCase,
    TaskCaseRunner,
)
from .registry import (
    RegisteredTaskAdapter,
    TaskAdapterRegistry,
    load_builtin_task_adapters,
    load_plugin_task_adapters,
    register_task_adapter,
    task_adapter_registry,
)
from .evaluator import evaluate_task_adapters

__all__ = [
    "BaseTaskAdapter",
    "ConfiguredTaskAdapter",
    "RegisteredTaskAdapter",
    "TaskAdapter",
    "TaskAdapterDescriptor",
    "TaskAdapterRegistry",
    "TaskAdapterRunContext",
    "TaskCase",
    "TaskCaseRunner",
    "evaluate_task_adapters",
    "load_builtin_task_adapters",
    "load_plugin_task_adapters",
    "register_task_adapter",
    "task_adapter_registry",
]
