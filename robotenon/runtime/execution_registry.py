"""Execution backend assembly; providers own their dispatch implementation."""
from dataclasses import dataclass
from typing import Any

from ..contracts.providers import ProviderRegistry

execution_registry = ProviderRegistry("execution", ("execute", "close"))
register_execution_backend = execution_registry.register


@dataclass(frozen=True)
class ExecutionBackendContext:
    options: Any
    profile: Any
    make_request: Any


@register_execution_backend("in_process", capabilities={"local_worker"})
def _sequential(*, context):
    from .execution import SequentialExecutionBackend
    return SequentialExecutionBackend()


@register_execution_backend("process", capabilities={"shared_observability", "managed_workers"})
def _process(*, context):
    from .parallel import PersistentExecutionBackend
    return PersistentExecutionBackend(context.options, context.profile, context.make_request)


def execution_provider(options):
    execution_registry.load(options.plugin_backends)
    return execution_registry.get(options.backend)


def build_execution_backend(options, profile, make_request):
    execution_provider(options)
    result = execution_registry.build(options.backend, params=options.backend_params,
        context=ExecutionBackendContext(options, profile, make_request))
    if getattr(result, "max_workers", None) != options.max_workers:
        raise ValueError("execution provider max_workers must match configured max_workers")
    return result
