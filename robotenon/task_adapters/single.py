"""The one-case adapter used by physical-robot and simple deployments."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..infra.params import Params
from .base import BaseTaskAdapter, TaskAdapterDescriptor, TaskCase
from .registry import register_task_adapter


def _read(params: Params, name: str, default: Any) -> Any:
    value = params.resolve(name)
    return default if value is None else value


@register_task_adapter(
    "single-task",
    version="1.0.0",
    kind="single",
    description="Run one configured task case, suitable for a physical robot.",
)
class SingleTaskAdapter(BaseTaskAdapter):
    descriptor = TaskAdapterDescriptor(
        adapter_id="single-task",
        version="1.0.0",
        display_name="Single Task",
        kind="single",
        capabilities=frozenset({"single_case"}),
    )

    def __init__(
        self,
        *,
        task_id: str | None = None,
        environment_id: str | None = None,
        seed: int | None = None,
        horizon: int | None = None,
        num_trials: int = 1,
        **parameters: Any,
    ) -> None:
        self.task_id = task_id
        self.environment_id = environment_id
        self.seed = seed
        self.horizon = horizon
        self.num_trials = int(num_trials)
        self.parameters = dict(parameters)

    def validate(self, params: Params) -> None:
        task_id = self.task_id or _read(params, "task.task_id", None)
        environment_id = self.environment_id or _read(params, "task.env_id", None)
        if not task_id or not environment_id:
            raise ValueError("single-task requires task_id and environment_id")
        if self.num_trials < 1:
            raise ValueError("single-task num_trials must be at least 1")
        if self.seed is not None:
            int(self.seed)
        if self.horizon is not None and int(self.horizon) < 1:
            raise ValueError("single-task horizon must be at least 1")

    def enumerate_cases(
        self,
        *,
        adapter_id: str,
        params: Params,
    ) -> Iterator[TaskCase]:
        self.validate(params)
        task_id = self.task_id or _read(params, "task.task_id", None)
        environment_id = self.environment_id or _read(params, "task.env_id", None)
        seed = self.seed if self.seed is not None else _read(params, "task.seed", 0)
        horizon = self.horizon if self.horizon is not None else _read(params, "runtime.horizon", None)
        for episode in range(self.num_trials):
            yield TaskCase(
                adapter_id=adapter_id,
                task_id=str(task_id),
                environment_id=str(environment_id),
                index=episode,
                episode=episode,
                seed=int(seed) + episode if seed is not None else None,
                horizon=int(horizon) if horizon is not None else None,
                parameters=dict(self.parameters),
            )


__all__ = ["SingleTaskAdapter"]
