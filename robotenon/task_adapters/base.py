"""Task enumeration and execution boundaries.

Task adapters sit between an experiment configuration and an environment
session.  They decide *which* task cases an experiment contains; the
environment adapter still decides *how* one case talks to a simulator or a
physical robot.  Keeping those concerns separate lets a dataset adapter
expand to hundreds of cases while a real-robot adapter yields one case.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..infra.params import Params


@dataclass(frozen=True, slots=True)
class TaskAdapterDescriptor:
    """Static metadata exposed by a registered task adapter."""

    adapter_id: str
    version: str = "1.0.0"
    display_name: str | None = None
    kind: str = "generic"
    capabilities: frozenset[str] = frozenset()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskCase:
    """One deterministic unit of work emitted by a :class:`TaskAdapter`.

    ``parameters["environment_parameters"]`` optionally supplies provider-owned
    session options, recursively overriding config ``environment.parameters``.
    Other parameters remain task-only. Keep all options JSON serializable.
    """

    adapter_id: str
    task_id: str
    environment_id: str
    index: int
    episode: int = 0
    seed: int | None = None
    horizon: int | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable human-readable key used in status and summary files."""

        # index is unique within an adapter, including repeated tasks/seeds.
        return f"{self.adapter_id}:{self.index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "task_id": self.task_id,
            "environment_id": self.environment_id,
            "index": self.index,
            "episode": self.episode,
            "seed": self.seed,
            "horizon": self.horizon,
            "parameters": dict(self.parameters),
            "metadata": dict(self.metadata),
            "key": self.key,
        }


TaskCaseRunner = Callable[
    [TaskCase, Path, Path | None, Path | None], Awaitable[Mapping[str, Any]]
]


@dataclass(slots=True)
class TaskAdapterRunContext:
    """Resources and the single-case executor shared with an adapter."""

    params: Params
    config_path: Path
    output_root: Path
    run_id: str
    memory_path: Path | None
    run_episode: TaskCaseRunner
    options: Mapping[str, Any] = field(default_factory=dict)
    case_output: Path | None = None
    case_trace_dir: Path | None = None


class TaskAdapter(Protocol):
    """Protocol implemented by registered task adapters."""

    descriptor: TaskAdapterDescriptor

    def describe(self) -> TaskAdapterDescriptor: ...

    def validate(self, params: Params) -> None: ...

    def enumerate_cases(
        self,
        *,
        adapter_id: str,
        params: Params,
    ) -> Iterator[TaskCase]: ...

    async def run_case(
        self,
        case: TaskCase,
        context: TaskAdapterRunContext,
    ) -> Mapping[str, Any]: ...


class BaseTaskAdapter:
    """Convenience base class for adapters with the default episode runner."""

    descriptor = TaskAdapterDescriptor(adapter_id="generic")

    def describe(self) -> TaskAdapterDescriptor:
        return self.descriptor

    def validate(self, params: Params) -> None:
        """Validate configuration without importing an environment or enumerating data."""
        return

    def enumerate_cases(
        self,
        *,
        adapter_id: str,
        params: Params,
    ) -> Iterator[TaskCase]:
        raise NotImplementedError

    async def run_case(
        self,
        case: TaskCase,
        context: TaskAdapterRunContext,
    ) -> Mapping[str, Any]:
        """Delegate one case to the experiment's environment-specific runner."""

        return await context.run_episode(
            case,
            context.case_output or context.output_root,
            context.case_trace_dir,
            context.memory_path,
        )


@dataclass(frozen=True, slots=True)
class ConfiguredTaskAdapter:
    """An ordered adapter instance built from one config item."""

    instance_id: str
    config: Any
    adapter: TaskAdapter


__all__ = [
    "BaseTaskAdapter",
    "ConfiguredTaskAdapter",
    "TaskAdapter",
    "TaskAdapterDescriptor",
    "TaskAdapterRunContext",
    "TaskCase",
    "TaskCaseRunner",
]
